"""Strava webhook receiver.

On Strava, you register **one push subscription per app** (not per user).
When any of your authorized athletes uploads, updates, or deletes an
activity, Strava POSTs a small JSON payload to your callback URL. We
respond 200 (within 2 seconds, per Strava's docs) and enqueue a job to
the worker that pulls the activity detail + streams.

What's here:
- GET  /strava/webhook   — subscription verification handshake
- POST /strava/webhook   — event receiver

What's NOT here (admin one-time setup once deployed with HTTPS):
- Creating the subscription itself. That's a single curl after deploy:

    curl -X POST https://www.strava.com/api/v3/push_subscriptions \\
        -F client_id=$STRAVA_CLIENT_ID -F client_secret=$STRAVA_CLIENT_SECRET \\
        -F callback_url=https://yourdomain.fly.dev/strava/webhook \\
        -F verify_token=$STRAVA_VERIFY_TOKEN

  Strava will GET the callback_url once with hub.challenge; we echo it back.

Local-dev caveat: webhooks require a publicly reachable HTTPS URL. They
are inert when the app runs at localhost:5001. The endpoints are still
mounted (so they boot cleanly) and unit-tests don't need real Strava
traffic to exercise them.
"""
from __future__ import annotations

import logging
import os
from flask import Blueprint, abort, jsonify, request

import storage
import worker

log = logging.getLogger(__name__)

bp = Blueprint("webhook", __name__)


@bp.route("/strava/webhook", methods=["GET"])
def verify():
    """Strava's subscription handshake.

    When you POST /push_subscriptions, Strava immediately GETs your callback
    URL with hub.mode=subscribe, hub.verify_token=<your_token>, hub.challenge=<random>.
    We must echo hub.challenge back in JSON.
    """
    expected_token = os.environ.get("STRAVA_VERIFY_TOKEN", "")
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode != "subscribe" or token != expected_token or not challenge:
        log.warning("webhook verify rejected: mode=%s token_match=%s", mode, token == expected_token)
        abort(400)
    return jsonify({"hub.challenge": challenge})


@bp.route("/strava/webhook", methods=["POST"])
def receive():
    """Receive an activity / athlete event from Strava.

    Payload shape (per Strava docs):
        {
          "aspect_type": "create" | "update" | "delete",
          "event_time": 1516126040,
          "object_id":   123456789,   # activity_id when object_type==activity
          "object_type": "activity" | "athlete",
          "owner_id":    987654321,   # the athlete_id
          "subscription_id": 1,
          "updates": {...}            # only present for athlete deauthorisation
        }
    """
    payload = request.get_json(silent=True) or {}
    try:
        aspect_type = payload["aspect_type"]
        object_type = payload["object_type"]
        object_id = int(payload["object_id"])
        owner_id = int(payload["owner_id"])
    except (KeyError, TypeError, ValueError):
        log.warning("webhook bad payload: %s", payload)
        # Strava still wants a 200 — they retry on non-200, which would spam.
        return jsonify({"ok": True})

    conn = storage.connect()
    try:
        user = storage.get_user_by_athlete_id(conn, owner_id)
    finally:
        conn.close()
    if user is None:
        log.info("webhook for unknown athlete %s — ignoring", owner_id)
        return jsonify({"ok": True})

    if object_type == "activity":
        # Defer the actual fetch to the worker so we respond within 2 s.
        worker.submit_sync_one(user.id, object_id, aspect_type=aspect_type)
    elif object_type == "athlete":
        # Athlete deauthorisation: updates = {"authorized": false}
        if (payload.get("updates") or {}).get("authorized") in (False, "false"):
            conn = storage.connect()
            try:
                conn.execute(
                    "UPDATE users SET deauthorized_at = datetime('now') WHERE id = ?",
                    (user.id,),
                )
                conn.commit()
            finally:
                conn.close()
            log.info("athlete %s deauthorized — marked user %s", owner_id, user.id)

    return jsonify({"ok": True})


def init_app(app) -> None:
    app.register_blueprint(bp)
