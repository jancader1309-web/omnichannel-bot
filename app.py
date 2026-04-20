"""
Barber Shop AI Bot — bez n8n
Messenger + Claude AI + Google Calendar + Telegram powiadomienia
"""

import os
import json
import hashlib
import hmac
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict

import requests
from flask import Flask, request, jsonify
from anthropic import Anthropic
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import pickle

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

VERIFY_TOKEN       = os.environ.get("FB_VERIFY_TOKEN", "barbershop2026")
PAGE_ACCESS_TOKEN  = os.environ["FB_PAGE_ACCESS_TOKEN"]
APP_SECRET         = os.environ["FB_APP_SECRET"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

TIMEZONE = ZoneInfo("Europe/Warsaw")
conversation_history = defaultdict(list)
anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)


def send_telegram_notification(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
    except Exception as ex:
        logger.error(f"Blad Telegram: {ex}")


CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]

def get_calendar_service():
    creds = None
    if os.path.exists("token_calendar.pickle"):
        with open("token_calendar.pickle", "rb") as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", CALENDAR_SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token_calendar.pickle", "wb") as f:
            pickle.dump(creds, f)
    return build("calendar", "v3", credentials=creds)


def get_calendar_events(time_min, time_max):
    try:
        service = get_calendar_service()
        result = service.events().list(
            calendarId="primary", timeMin=time_min, timeMax=time_max,
            singleEvents=True, orderBy="startTime",
        ).execute()
        formatted = []
        for e in result.get("items", []):
            formatted.append({
                "id": e["id"],
                "summary": e.get("summary", "(brak tytulu)"),
                "start": e["start"].get("dateTime", e["start"].get("date")),
                "end": e["end"].get("dateTime", e["end"].get("date")),
            })
        return formatted
    except Exception as ex:
        logger.error(f"Blad kalendarza: {ex}")
        return []


def create_calendar_event(summary, start, end):
    try:
        service = get_calendar_service()
        event = {
            "summary": summary,
            "start": {"dateTime": start, "timeZone": "Europe/Warsaw"},
            "end": {"dateTime": end, "timeZone": "Europe/Warsaw"},
        }
        created = service.events().insert(calendarId="primary", body=event).execute()
        return {"id": created["id"], "summary": created.get("summary")}
    except Exception as ex:
        return {"error": str(ex)}


def delete_calendar_event(event_id):
    try:
        service = get_calendar_service()
        service.events().delete(calendarId="primary", eventId=event_id).execute()
        return True
    except Exception as ex:
        logger.error(f"Blad usuwania: {ex}")
        return False


TOOLS = [
    {
        "name": "get_calendar_events",
        "description": "Pobiera wydarzenia z Google Calendar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "time_min": {"type": "string", "description": "Data poczatkowa ISO 8601"},
                "time_max": {"type": "string", "description": "Data koncowa ISO 8601"},
            },
            "required": ["time_min", "time_max"],
        },
    },
    {
        "name": "create_calendar_event",
        "description": "Tworzy nowe wydarzenie w kalendarzu po potwierdzeniu przez klienta.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Tytul: Wizyta - Imie Nazwisko - usluga - tel.numer"},
                "start": {"type": "string", "description": "Czas rozpoczecia ISO 8601 z +02:00"},
                "end": {"type": "string", "description": "Czas zakonczenia ISO 8601 z +02:00"},
            },
            "required": ["summary", "start", "end"],
        },
    },
    {
        "name": "delete_calendar_event",
        "description": "Usuwa wydarzenie z kalendarza po weryfikacji telefonu.",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "ID wydarzenia"},
            },
            "required": ["event_id"],
        },
    },
]


def handle_tool(name, inputs):
    if name == "get_calendar_events":
        events = get_calendar_events(inputs["time_min"], inputs["time_max"])
        if not events:
            return "Brak wydarzen w podanym przedziale — termin jest wolny."
        return json.dumps(events, ensure_ascii=False)

    elif name == "create_calendar_event":
        result = create_calendar_event(inputs["summary"], inputs["start"], inputs["end"])
        if "error" in result:
            return "Blad tworzenia rezerwacji: " + result["error"]
        from datetime import datetime
        dt = datetime.fromisoformat(inputs["start"])
        data_godz = dt.strftime("%d-%m-%Y, godz. %H:%M")
        send_telegram_notification("🟢 Nowa rezerwacja!\n" + inputs["summary"] + "\n" + data_godz)
        return "Rezerwacja utworzona. ID: " + result["id"]

    elif name == "delete_calendar_event":
        ok = delete_calendar_event(inputs["event_id"])
        if ok:
            send_telegram_notification("Wizyta zostala odwolana przez klienta.")
        return "Wizyta odwolana pomyslnie." if ok else "Nie udalo sie odwolac wizyty."

    return "Nieznane narzedzie: " + name


def build_system_message():
    now = datetime.now(TIMEZONE)
    days = "\n".join((now + timedelta(days=i)).strftime("%A %d %B %Y") for i in range(36))
    return """Jestes asystentem Barber Shop Praga. Mow po polsku.

SALON:
Adres: ul. Zabkowska 38, Warszawa | Tel: 739-299-091
Pon-Pt: 8-21 | Sob: 8-18 | Niedz: 9-15 (wybrane)

CENNIK:
Strzyzenie wlosow: 100 zl (40 min)
Strzyzenie dlugich wlosow: 120 zl (60 min)
Strzyzenie + broda: 160 zl (80 min)
Strzyzenie brody: 80 zl (40 min)
Golenie brody: 80 zl (40 min)
Strzyzenie dzieci 3-11 lat: 100 zl (40 min)
Modelowanie + mycie: 40 zl (10 min)
Depilacja nosa/uszu: 30 zl (10 min)

Dzisiaj i najblizsze 35 dni:
""" + days + """

REZERWACJA:
1. Zbierz: imie i nazwisko, telefon, usluge, dzien i godzine
2. Uzyj get_calendar_events — sprawdz czy termin wolny
3. Powiedz: Rezerwuje: [dzien] [data] o [godzina] - [usluga]. Czy potwierdzasz?
4. Po tak — uzyj create_calendar_event
   Tytul: Wizyta - [Imie Nazwisko] - [usluga] - tel.[telefon]
   Daty zawsze z +02:00, np. 2026-04-17T10:00:00+02:00
5. Potwierdz klientowi

ODWOLANIE:
1. Zbierz: imie, telefon, date
2. get_calendar_events — znajdz wizyte
3. Sprawdz telefon w tytule — jesli pasuje uzyj delete_calendar_event

WOLNE TERMINY:
get_calendar_events na wybrany dzien — pokaz wolne sloty co 40 min

FORMATOWANIE: Uzywaj emoji, unikaj gwiazdek i myslnikow.
"""


def run_agent(sender_id, user_message):
    history = conversation_history[sender_id]
    history.append({"role": "user", "content": user_message})
    if len(history) > 20:
        history = history[-20:]
        conversation_history[sender_id] = history

    while True:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=build_system_message(),
            tools=TOOLS,
            messages=history,
        )

        if response.stop_reason == "end_turn":
            text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    text += block.text
            history.append({"role": "assistant", "content": response.content})
            return text or "Przepraszam, sprobuj ponownie."

        elif response.stop_reason == "tool_use":
            history.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = handle_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            history.append({"role": "user", "content": tool_results})
        else:
            return "Przepraszam, wystapil blad."


def send_message(recipient_id, text):
    chunks = [text[i:i+1900] for i in range(0, len(text), 1900)]
    for chunk in chunks:
        resp = requests.post(
            "https://graph.facebook.com/v25.0/me/messages",
            params={"access_token": PAGE_ACCESS_TOKEN},
            json={"recipient": {"id": recipient_id}, "messaging_type": "RESPONSE", "message": {"text": chunk}},
        )
        if resp.status_code != 200:
            logger.error(f"Blad wysylania: {resp.text}")


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def handle_webhook():
    data = request.json
    if data.get("object") != "page":
        return "OK", 200
    for entry in data.get("entry", []):
        for messaging in entry.get("messaging", []):
            sender_id = messaging["sender"]["id"]
            text = messaging.get("message", {}).get("text", "")
            if not text:
                continue
            logger.info(f"Wiadomosc od {sender_id}: {text}")
                    send_message(sender_id, "⏳")
                    reply = run_agent(sender_id, text)
                    send_message(sender_id, reply)
    return "OK", 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": datetime.now(TIMEZONE).isoformat()})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
