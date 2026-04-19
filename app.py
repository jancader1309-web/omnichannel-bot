"""
Barber Shop AI Bot — bez n8n
Messenger + Claude AI + Google Calendar
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

# ── Konfiguracja ──────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

VERIFY_TOKEN      = os.environ.get("FB_VERIFY_TOKEN", "barbershop2026")
PAGE_ACCESS_TOKEN = os.environ["FB_PAGE_ACCESS_TOKEN"]
APP_SECRET        = os.environ["FB_APP_SECRET"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

TIMEZONE = ZoneInfo("Europe/Warsaw")

# Pamięć konwersacji — osobna dla każdego klienta
conversation_history = defaultdict(list)

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Google Calendar ───────────────────────────────────────────────────────────

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
            flow = InstalledAppFlow.from_client_secrets_file(
                "credentials.json", CALENDAR_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open("token_calendar.pickle", "wb") as f:
            pickle.dump(creds, f)
    return build("calendar", "v3", credentials=creds)


def get_calendar_events(time_min: str, time_max: str) -> list:
    """Pobiera wydarzenia z kalendarza w podanym przedziale czasu."""
    try:
        service = get_calendar_service()
        result = service.events().list(
            calendarId="primary",
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        events = result.get("items", [])
        formatted = []
        for e in events:
            start = e["start"].get("dateTime", e["start"].get("date"))
            end   = e["end"].get("dateTime",   e["end"].get("date"))
            formatted.append({
                "id":      e["id"],
                "summary": e.get("summary", "(brak tytułu)"),
                "start":   start,
                "end":     end,
            })
        return formatted
    except Exception as ex:
        logger.error(f"Błąd pobierania wydarzeń: {ex}")
        return []


def create_calendar_event(summary: str, start: str, end: str) -> dict:
    """Tworzy nowe wydarzenie w kalendarzu."""
    try:
        service = get_calendar_service()
        event = {
            "summary": summary,
            "start": {"dateTime": start, "timeZone": "Europe/Warsaw"},
            "end":   {"dateTime": end,   "timeZone": "Europe/Warsaw"},
            "reminders": {
                "useDefault": False,
                "overrides": [{"method": "popup", "minutes": 60}],
            },
        }
        created = service.events().insert(calendarId="primary", body=event).execute()
        return {"id": created["id"], "summary": created.get("summary")}
    except Exception as ex:
        logger.error(f"Błąd tworzenia wydarzenia: {ex}")
        return {"error": str(ex)}


def delete_calendar_event(event_id: str) -> bool:
    """Usuwa wydarzenie z kalendarza."""
    try:
        service = get_calendar_service()
        service.events().delete(calendarId="primary", eventId=event_id).execute()
        return True
    except Exception as ex:
        logger.error(f"Błąd usuwania wydarzenia: {ex}")
        return False


# ── Narzędzia dla Claude ──────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "get_calendar_events",
        "description": "Pobiera wydarzenia z Google Calendar w podanym przedziale czasu. Używaj do sprawdzania wolnych terminów.",
        "input_schema": {
            "type": "object",
            "properties": {
                "time_min": {"type": "string", "description": "Data początkowa ISO 8601, np. 2026-04-17T00:00:00+02:00"},
                "time_max": {"type": "string", "description": "Data końcowa ISO 8601, np. 2026-04-17T23:59:59+02:00"},
            },
            "required": ["time_min", "time_max"],
        },
    },
    {
        "name": "create_calendar_event",
        "description": "Tworzy nowe wydarzenie w Google Calendar. Używaj po potwierdzeniu rezerwacji przez klienta.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Tytuł: 'Wizyta - [Imię Nazwisko] - [usługa] - tel.[telefon]'"},
                "start":   {"type": "string", "description": "Czas rozpoczęcia ISO 8601 z +02:00"},
                "end":     {"type": "string", "description": "Czas zakończenia ISO 8601 z +02:00"},
            },
            "required": ["summary", "start", "end"],
        },
    },
    {
        "name": "delete_calendar_event",
        "description": "Usuwa wydarzenie z Google Calendar. Używaj po weryfikacji numeru telefonu klienta.",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "ID wydarzenia do usunięcia"},
            },
            "required": ["event_id"],
        },
    },
]


def handle_tool(name: str, inputs: dict) -> str:
    if name == "get_calendar_events":
        events = get_calendar_events(inputs["time_min"], inputs["time_max"])
        if not events:
            return "Brak wydarzeń w podanym przedziale — termin jest wolny."
        return json.dumps(events, ensure_ascii=False)

    elif name == "create_calendar_event":
        result = create_calendar_event(inputs["summary"], inputs["start"], inputs["end"])
        if "error" in result:
            return f"Błąd tworzenia rezerwacji: {result['error']}"
        return f"Rezerwacja utworzona. ID: {result['id']}"

    elif name == "delete_calendar_event":
        ok = delete_calendar_event(inputs["event_id"])
        return "Wizyta odwołana pomyślnie." if ok else "Nie udało się odwołać wizyty."

    return f"Nieznane narzędzie: {name}"


# ── System Message ────────────────────────────────────────────────────────────

def build_system_message() -> str:
    now = datetime.now(TIMEZONE)
    days = "\n".join(
        (now + timedelta(days=i)).strftime("%A %d %B %Y")
        for i in range(36)
    )
    return f"""Jesteś asystentem Barber Shop Praga. Mów po polsku.

SALON:
Adres: ul. Ząbkowska 38, Warszawa | Tel: 739-299-091
Pon-Pt: 8-21 | Sob: 8-18 | Niedz: 9-15 (wybrane)

CENNIK:
✂️ Strzyżenie włosów: 100 zł (40 min)
✂️ Strzyżenie długich włosów: 120 zł (60 min)
✂️🧔 Strzyżenie + broda: 160 zł (80 min)
🧔 Strzyżenie brody: 80 zł (40 min)
🧔 Golenie brody: 80 zł (40 min)
👦 Strzyżenie dzieci 3-11 lat: 100 zł (40 min)
💈 Modelowanie + mycie: 40 zł (10 min)
👃 Depilacja nosa/uszu: 30 zł (10 min)

Dzisiaj i najbliższe 35 dni:
{days}

REZERWACJA:
1. Zbierz: imię i nazwisko, telefon, usługę, dzień i godzinę
2. Użyj get_calendar_events — sprawdź czy termin wolny
3. Powiedz: "Rezerwuję: [dzień] [data] o [godzina] - [usługa]. Czy potwierdzasz?"
4. Po "tak" — użyj create_calendar_event
   Tytuł: "Wizyta - [Imię Nazwisko] - [usługa] - tel.[telefon]"
   Daty zawsze z +02:00, np. 2026-04-17T10:00:00+02:00
5. Potwierdź klientowi

ODWOŁANIE:
1. Zbierz: imię, telefon, datę
2. get_calendar_events — znajdź wizytę
3. Sprawdź telefon w tytule — jeśli pasuje użyj delete_calendar_event

WOLNE TERMINY:
get_calendar_events na wybrany dzień → pokaż wolne sloty co 40 min (✅ wolne ❌ zajęte)

FORMATOWANIE: Używaj emoji, unikaj gwiazdek i myślników.
"""


# ── Agent Claude ──────────────────────────────────────────────────────────────

def run_agent(sender_id: str, user_message: str) -> str:
    """Główna pętla agenta z tool use."""
    history = conversation_history[sender_id]
    history.append({"role": "user", "content": user_message})

    # Ogranicz historię do ostatnich 20 wiadomości
    if len(history) > 20:
        history = history[-20:]
        conversation_history[sender_id] = history

    while True:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6-20250217",
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
            return text or "Przepraszam, spróbuj ponownie."

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
            return "Przepraszam, wystąpił błąd. Spróbuj ponownie."


# ── Messenger API ─────────────────────────────────────────────────────────────

def send_message(recipient_id: str, text: str) -> None:
    """Wysyła wiadomość przez Messenger API."""
    # Podziel długie wiadomości na części (Messenger limit: 2000 znaków)
    chunks = [text[i:i+1900] for i in range(0, len(text), 1900)]
    for chunk in chunks:
        payload = {
            "recipient": {"id": recipient_id},
            "messaging_type": "RESPONSE",
            "message": {"text": chunk},
        }
        resp = requests.post(
            f"https://graph.facebook.com/v25.0/me/messages",
            params={"access_token": PAGE_ACCESS_TOKEN},
            json=payload,
        )
        if resp.status_code != 200:
            logger.error(f"Błąd wysyłania wiadomości: {resp.text}")


def verify_signature(payload: bytes, signature: str) -> bool:
    """Weryfikuje podpis webhooka od Meta."""
    if not signature.startswith("sha256="):
        return False
    expected = hmac.new(
        APP_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature[7:])


# ── Endpointy Flask ───────────────────────────────────────────────────────────

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Weryfikacja webhooka przez Meta."""
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        logger.info("Webhook zweryfikowany!")
        return challenge, 200
    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def handle_webhook():
    """Odbiera wiadomości z Messengera."""
    # Weryfikacja podpisu
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_signature(request.data, signature):
        logger.warning("Nieprawidłowy podpis!")
        return "Forbidden", 403

    data = request.json
    if data.get("object") != "page":
        return "OK", 200

    for entry in data.get("entry", []):
        for messaging in entry.get("messaging", []):
            sender_id = messaging["sender"]["id"]
            message   = messaging.get("message", {})
            text      = message.get("text", "")

            if not text:
                continue

            logger.info(f"Wiadomość od {sender_id}: {text}")

            # Odpowiedz natychmiast (Meta wymaga odpowiedzi w 20s)
            # Przetwarzanie w tym samym wątku dla prostoty
            reply = run_agent(sender_id, text)
            send_message(sender_id, reply)

    return "OK", 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": datetime.now(TIMEZONE).isoformat()})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
