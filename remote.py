#!/usr/bin/env python3
"""
remote.py — send commands to your MyControl phone from Termux.

Setup (once):
    pkg install python
    pip install requests google-auth
    cp config.example.json config.json
    # fill in config.json (see SETUP.md)
    # place your Firebase service account JSON as service-account.json

Usage:
    ./remote.py                # interactive dashboard — pick an action from a menu
    ./remote.py ping           # or run a single action directly, for scripting
    ./remote.py send_sms --to +1234567890 --text "hello from termux"
    ./remote.py toggle_flashlight --on true
    ./remote.py take_photo
"""

import argparse
import json
import os
import sys
import time
import uuid

import requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleAuthRequest

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
AUTH_CACHE_PATH = os.path.join(os.path.dirname(__file__), ".auth_cache.json")
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

DEFAULT_PROJECT_ID = "control-f5136"
DEFAULT_API_KEY = "AIzaSyDh0Wd-GTSyWFN7ytVbdBShcja3ybSg3lU"
DEFAULT_SERVICE_ACCOUNT_PATH = "service-account.json"


def load_config():
    if not os.path.exists(CONFIG_PATH):
        print("First run — setting up config.json (only needs the device ID).")
        device_id = input("Paste the Device ID shown in the MyControl app: ").strip()
        config = {
            "projectId": DEFAULT_PROJECT_ID,
            "apiKey": DEFAULT_API_KEY,
            "serviceAccountKeyPath": DEFAULT_SERVICE_ACCOUNT_PATH,
            "deviceId": device_id,
        }
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=2)
        return config

    with open(CONFIG_PATH) as f:
        return json.load(f)


def firestore_base(project_id):
    return f"https://firestore.googleapis.com/v1/projects/{project_id}/databases/(default)/documents"


def sign_in(api_key):
    """Anonymous Firebase Auth sign-in (for Firestore reads/writes), cached with refresh."""
    if os.path.exists(AUTH_CACHE_PATH):
        with open(AUTH_CACHE_PATH) as f:
            cache = json.load(f)
        resp = requests.post(
            f"https://securetoken.googleapis.com/v1/token?key={api_key}",
            data={"grant_type": "refresh_token", "refresh_token": cache["refreshToken"]},
        )
        if resp.ok:
            data = resp.json()
            token = data["id_token"]
            _cache_auth(token, data["refresh_token"])
            return token

    resp = requests.post(
        f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={api_key}",
        json={"returnSecureToken": True},
    )
    resp.raise_for_status()
    data = resp.json()
    _cache_auth(data["idToken"], data["refreshToken"])
    return data["idToken"]


def _cache_auth(id_token, refresh_token):
    with open(AUTH_CACHE_PATH, "w") as f:
        json.dump({"idToken": id_token, "refreshToken": refresh_token}, f)


def get_fcm_access_token(service_account_path):
    """OAuth2 access token for the FCM HTTP v1 API, derived from the service account key."""
    if not os.path.exists(service_account_path):
        sys.exit(
            f"Missing service account key at {service_account_path} — "
            "download it from Firebase console > Project settings > Service accounts."
        )
    creds = service_account.Credentials.from_service_account_file(
        service_account_path, scopes=[FCM_SCOPE]
    )
    creds.refresh(GoogleAuthRequest())
    return creds.token


def to_firestore_value(value):
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, (int, float)):
        return {"integerValue": str(value)}
    return {"stringValue": str(value)}


def create_command(project_id, id_token, device_id, action, params):
    command_id = uuid.uuid4().hex[:12]
    fields = {
        "deviceId": {"stringValue": device_id},
        "action": {"stringValue": action},
        "status": {"stringValue": "pending"},
        "createdAt": {"integerValue": str(int(time.time() * 1000))},
        "params": {"mapValue": {"fields": {k: to_firestore_value(v) for k, v in params.items()}}},
    }
    url = f"{firestore_base(project_id)}/commands/{command_id}"
    resp = requests.patch(
        url,
        headers={"Authorization": f"Bearer {id_token}"},
        json={"fields": fields},
    )
    resp.raise_for_status()
    return command_id


def get_device_token(project_id, id_token, device_id):
    url = f"{firestore_base(project_id)}/devices/{device_id}"
    resp = requests.get(url, headers={"Authorization": f"Bearer {id_token}"})
    resp.raise_for_status()
    fields = resp.json().get("fields", {})
    return fields.get("fcmToken", {}).get("stringValue")


def wake_device(project_id, fcm_access_token, fcm_token, command_id):
    url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {fcm_access_token}",
            "Content-Type": "application/json",
        },
        json={
            "message": {
                "token": fcm_token,
                "data": {"commandId": command_id},
                "android": {"priority": "high"},
            }
        },
    )
    resp.raise_for_status()


def poll_result(project_id, id_token, command_id, timeout=60):
    url = f"{firestore_base(project_id)}/results/{command_id}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = requests.get(url, headers={"Authorization": f"Bearer {id_token}"})
        if resp.status_code == 200:
            fields = resp.json().get("fields", {})
            status = fields.get("status", {}).get("stringValue")
            output = fields.get("output", {}).get("stringValue", "")
            return status, output
        time.sleep(1.5)
    return "timeout", "no response from device within timeout"


def send_and_wait(config, action, params):
    """Sends one command and waits for the result. Shared by CLI mode and the menu."""
    id_token = sign_in(config["apiKey"])

    command_id = create_command(config["projectId"], id_token, config["deviceId"], action, params)

    fcm_token = get_device_token(config["projectId"], id_token, config["deviceId"])
    if not fcm_token:
        print("No FCM token registered for this device yet — open MyControl on phone 2 first.")
        return

    service_account_path = os.path.join(os.path.dirname(__file__), config["serviceAccountKeyPath"])
    fcm_access_token = get_fcm_access_token(service_account_path)
    wake_device(config["projectId"], fcm_access_token, fcm_token, command_id)

    print(f"sent '{action}' ({command_id}), waiting for result…")
    status, output = poll_result(config["projectId"], id_token, command_id)
    print(f"[{status}] {output}")


MENU_ACTIONS = [
    ("Ping", "ping"),
    ("List SIMs", "list_sims"),
    ("Send SMS", "send_sms"),
    ("Toggle flashlight", "toggle_flashlight"),
    ("Take photo", "take_photo"),
    ("Vibrate", "vibrate"),
    ("Find phone (play sound)", "find_phone"),
    ("Get device info", "get_device_info"),
    ("Get network info", "get_network_info"),
    ("Open URL", "open_url"),
    ("Set volume", "set_volume"),
    ("Lock screen", "lock_screen"),
    ("List installed apps", "get_installed_apps"),
    ("Set ringer mode", "set_ringer_mode"),
    ("Show message on screen", "show_message"),
    ("Delete SMS", "delete_sms"),
]


def parse_list_sms_output(output):
    """Parse the list_sms result into a list of dicts with number, id, sender, date, snippet."""
    lines = output.split("\n")
    # Skip first line (header like "inbox (50 messages, 1-50):")
    messages = []
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        # Format: [1] ID:1054 | +256779152355 | 07-22 19:45 | Hello,how was yr day
        try:
            # Extract list number
            num_part = line.split("]")[0].lstrip("[")
            list_num = int(num_part)

            # Extract ID
            id_part = line.split("ID:")[1].split(" |")[0]
            msg_id = id_part

            # Extract sender (between first | and second |)
            after_id = line.split("|", 1)[1].strip() if "|" in line else ""
            parts = after_id.split("|")
            sender = parts[0].strip() if len(parts) > 0 else "?"
            date = parts[1].strip() if len(parts) > 1 else "?"
            snippet = parts[2].strip() if len(parts) > 2 else "?"

            messages.append({
                "num": list_num,
                "id": msg_id,
                "sender": sender,
                "date": date,
                "snippet": snippet,
            })
        except (IndexError, ValueError):
            continue
    return messages


def run_delete_sms(config):
    """Interactive two-step flow: list messages, pick one with preview, delete it."""
    print("\n--- Delete SMS ---")

    # Step 1: Choose folder
    folder_choice = input("Which folder — (i)nbox or (s)ent [i]: ").strip().lower()
    folder = "sent" if folder_choice.startswith("s") else "inbox"

    # Step 2: List messages
    print(f"\nFetching last 50 messages from {folder}…")
    id_token = sign_in(config["apiKey"])

    command_id = create_command(config["projectId"], id_token, config["deviceId"], "list_sms", {"folder": folder})

    fcm_token = get_device_token(config["projectId"], id_token, config["deviceId"])
    if not fcm_token:
        print("No FCM token registered — open MyControl app first.")
        return

    service_account_path = os.path.join(os.path.dirname(__file__), config["serviceAccountKeyPath"])
    fcm_access_token = get_fcm_access_token(service_account_path)
    wake_device(config["projectId"], fcm_access_token, fcm_token, command_id)

    print(f"sent 'list_sms' ({command_id}), waiting…")
    status, output = poll_result(config["projectId"], id_token, command_id)

    if status != "ok":
        print(f"[{status}] {output}")
        return

    # Parse and display the messages
    messages = parse_list_sms_output(output)

    if not messages:
        print(f"[{status}] {output}")
        return

    print()  # blank line
    for msg in messages:
        print(f"[{msg['num']}] ID:{msg['id']} | {msg['sender']} | {msg['date']} | {msg['snippet']}")

    # Step 3: Pick which to delete
    print()
    choice = input("Enter the number of the message to delete (or 0 to cancel): ").strip()
    if choice == "0" or not choice.isdigit():
        print("Cancelled.")
        return

    choice_num = int(choice)

    # Find the message by list number
    selected = None
    for msg in messages:
        if msg["num"] == choice_num:
            selected = msg
            break

    if not selected:
        print(f"Could not find message number {choice_num}.")
        return

    # Step 4: Show preview and confirm
    print(f"\nMessage to delete:")
    print(f"  From: {selected['sender']}")
    print(f"  Date: {selected['date']}")
    print(f"  Text: {selected['snippet']}")
    confirm = input(f"\nDelete this message (ID: {selected['id']})? (y/N): ").strip().lower()
    if not confirm.startswith("y"):
        print("Cancelled.")
        return

    print(f"\nDeleting message {selected['id']}…")
    send_and_wait(config, "delete_sms", {"id": selected['id']})


def run_dashboard(config):
    while True:
        print("\nMyControl dashboard")
        for i, (label, _) in enumerate(MENU_ACTIONS, start=1):
            print(f"  {i}. {label}")
        print("  0. Exit")

        choice = input("> ").strip()
        if choice == "0":
            break
        if not choice.isdigit() or not (1 <= int(choice) <= len(MENU_ACTIONS)):
            print("Not a valid choice.")
            continue

        _, action = MENU_ACTIONS[int(choice) - 1]
        params = {}

        if action == "send_sms":
            params["to"] = input("Send to (phone number): ").strip()
            params["text"] = input("Message text: ").strip()
            sim = input("SIM slot — 1, 2, or blank for default: ").strip()
            if sim:
                params["sim"] = sim
        elif action == "toggle_flashlight":
            on = input("Turn on? (y/n): ").strip().lower()
            params["on"] = on.startswith("y")
        elif action == "take_photo":
            cam = input("Camera — (r)ear or (f)ront [r]: ").strip().lower()
            params["camera"] = "front" if cam.startswith("f") else "back"
        elif action == "open_url":
            params["url"] = input("URL to open: ").strip()
        elif action == "set_volume":
            params["level"] = input("Volume level (0-100): ").strip()
        elif action == "set_ringer_mode":
            mode = input("Mode — (n)ormal, (v)ibrate, or (s)ilent: ").strip().lower()
            params["mode"] = {"n": "normal", "v": "vibrate", "s": "silent"}.get(mode, mode)
        elif action == "show_message":
            params["text"] = input("Message to show: ").strip()
        elif action == "delete_sms":
            run_delete_sms(config)
            continue

        try:
            send_and_wait(config, action, params)
        except Exception as e:
            print(f"error: {e}")


def main():
    parser = argparse.ArgumentParser(description="Control your MyControl phone from Termux")
    parser.add_argument("action", nargs="?", help="ping | send_sms | toggle_flashlight | take_photo — omit for the interactive menu")
    parser.add_argument("--to")
    parser.add_argument("--text")
    parser.add_argument("--on")
    parser.add_argument("--camera", choices=["front", "back"], help="for take_photo")
    parser.add_argument("--sim", choices=["1", "2"], help="for send_sms on dual-SIM phones")
    parser.add_argument("--url", help="for open_url")
    parser.add_argument("--level", help="for set_volume (0-100)")
    parser.add_argument("--mode", choices=["normal", "vibrate", "silent"], help="for set_ringer_mode")
    parser.add_argument("--message", help="for show_message")
    args = parser.parse_args()

    config = load_config()

    if args.action is None:
        run_dashboard(config)
        return

    params = {}
    if args.to:
        params["to"] = args.to
    if args.text:
        params["text"] = args.text
    if args.on is not None:
        params["on"] = args.on.lower() == "true"
    if args.camera:
        params["camera"] = args.camera
    if args.sim:
        params["sim"] = args.sim
    if args.url:
        params["url"] = args.url
    if args.level:
        params["level"] = args.level
    if args.mode:
        params["mode"] = args.mode
    if args.message:
        params["text"] = args.message

    send_and_wait(config, args.action, params)


if __name__ == "__main__":
    main()