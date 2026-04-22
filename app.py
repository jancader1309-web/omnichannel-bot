"""
Barber Shop AI Bot
Messenger + Claude AI + Google Calendar + Google Sheets + Telegram + Przypomnienia + Opinie
"""

import os
import re
import json
import hashlib
import hmac
import logging
import pickle
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict

import requests
from flask import Flask, request, jsonify
from anthropic import Anthropic
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

VERIFY_TOKEN       = os.environ.get("FB_VERIFY_TOKEN", "barbershop2026")
PAGE_ACCESS_TOKEN  = os.environ["FB_PAGE_ACCESS_TOKEN"]
APP_SECRET         = os.environ["FB_APP_SECRET"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
SHEETS_ID          = os.environ.get("SHEETS_ID", "1WgecCrUjabsAS9KKLqV6UgxQMzsSPGGazD4DG8vmL1I")

TIMEZONE = ZoneInfo("Europe/Warsaw")
conversation_history = defaultdict(list)
awaiting_review = {}  # sender_id -> {step, imie, usluga, barber, ocena}
anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)
processed_messages = set()  # ochrona przed duplikatami

BARBERS = ["Daria", "Bozena", "Ola"]


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram_notification(text):
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


# ── Google API ────────────────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/spreadsheets",
]


def get_google_creds():
    creds = None
    if os.path.exists("token_calendar.pickle"):
        with open("token_calendar.pickle", "rb") as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token_calendar.pickle", "wb") as f:
            pickle.dump(creds, f)
    return creds


def get_calendar_service():
    return build("calendar", "v3", credentials=get_google_creds())


def get_sheets_service():
    return build("sheets", "v4", credentials=get_google_creds())


# ── Google Calendar ───────────────────────────────────────────────────────────

def get_calendar_events(time_min, time_max):
    try:
        service = get_calendar_service()
        result = service.events().list(
            calendarId="primary",
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
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


# ── Google Sheets — Opinie ────────────────────────────────────────────────────

def save_review(imie, usluga, barber, ocena, komentarz, messenger_id):
    try:
        service = get_sheets_service()
        now = datetime.now(TIMEZONE).strftime("%d.%m.%Y %H:%M")
        values = [[now, imie, usluga, barber, ocena, komentarz, messenger_id]]
        service.spreadsheets().values().append(
            spreadsheetId=SHEETS_ID,
            range="A:G",
            valueInputOption="RAW",
            body={"values": values},
        ).execute()
        logger.info(f"Opinia zapisana: {imie} - {ocena}/5")
        return True
    except Exception as ex:
        logger.error(f"Blad zapisu opinii: {ex}")
        return False


# ── Opinie — logika rozmowy ───────────────────────────────────────────────────

def handle_review_flow(sender_id, text):
    """Obsługuje rozmowę zbierania opinii — zwraca odpowiedź lub None jeśli nie dotyczy."""
    state = awaiting_review.get(sender_id)
    if not state:
        return None

    step = state.get("step")

    if step == "ocena":
        # Oczekujemy cyfry 1-5
        ocena = text.strip()
        if ocena not in ["1", "2", "3", "4", "5"]:
            return "Prosze wpisz cyfre od 1 do 5 ⭐"
        state["ocena"] = ocena
        state["step"] = "komentarz"
        awaiting_review[sender_id] = state
        gwiazdki = "⭐" * int(ocena)
        return f"Dziekujemy za ocene {gwiazdki}\n\nCzy chcesz dodac krotki komentarz? (napisz komentarz lub 'nie')"

    elif step == "komentarz":
        komentarz = "" if text.strip().lower() in ["nie", "no", "n"] else text.strip()
        imie = state.get("imie", "")
        usluga = state.get("usluga", "")
        barber = state.get("barber", "")
        ocena = state.get("ocena", "")

        save_review(imie, usluga, barber, ocena, komentarz, sender_id)

        # Powiadom wlasciciela
        gwiazdki = "⭐" * int(ocena)
        msg = f"Nowa opinia!\nKlient: {imie}\nBarber: {barber}\nUsluga: {usluga}\nOcena: {gwiazdki} ({ocena}/5)"
        if komentarz:
            msg += f"\nKomentarz: {komentarz}"
        send_telegram_notification(msg)

        del awaiting_review[sender_id]
        return f"Dziekujemy za opinie! To dla nas bardzo wazne 🙏\nDo zobaczenia w Barber Shop Praga! ✂️"

    return None


def start_review(sender_id, imie, usluga, barber):
    """Rozpoczyna zbieranie opinii dla klienta."""
    awaiting_review[sender_id] = {
        "step": "ocena",
        "imie": imie,
        "usluga": usluga,
        "barber": barber,
    }


# ── Przypomnienia ─────────────────────────────────────────────────────────────

def send_messenger_message_tagged(messenger_id, text):
    try:
        resp = requests.post(
            "https://graph.facebook.com/v25.0/me/messages",
            params={"access_token": PAGE_ACCESS_TOKEN},
            json={
                "recipient": {"id": messenger_id},
                "messaging_type": "MESSAGE_TAG",
                "tag": "CONFIRMED_EVENT_UPDATE",
                "message": {"text": text},
            },
            timeout=10,
        )
        if resp.status_code != 200:
            logger.error(f"Blad przypomnienia: {resp.text}")
    except Exception as ex:
        logger.error(f"Blad wysylania: {ex}")


def send_reminders():
    logger.info("Sprawdzam jutrzejsze wizyty...")
    now = datetime.now(TIMEZONE)
    tomorrow_start = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_end = tomorrow_start.replace(hour=23, minute=59, second=59)

    events = get_calendar_events(tomorrow_start.isoformat(), tomorrow_end.isoformat())
    if not events:
        logger.info("Brak wizyt na jutro.")
        return

    for event in events:
        summary = event.get("summary", "")
        start = event.get("start", "")

        mid_match = re.search(r"mid\.(\d+)", summary)
        if not mid_match:
            continue

        messenger_id = mid_match.group(1)

        try:
            dt = datetime.fromisoformat(start)
            godz = dt.strftime("%H:%M")
            data = dt.strftime("%d.%m.%Y")
        except Exception:
            godz = start
            data = ""

        name_match = re.search(r"Wizyta - ([^-]+) -", summary)
        imie = name_match.group(1).strip() if name_match else "Kliencie"

        parts = summary.split(" - ")
        usluga = parts[2].strip() if len(parts) > 2 else "wizyta"

        barber_match = re.search(r"barber\.([^-]+?)(?:\s*-|$)", summary)
        barber = barber_match.group(1).strip() if barber_match else ""
        barber_info = f"\nBarber: {barber}" if barber else ""

        reminder_text = (
            f"Czesc {imie}! Przypominamy o jutrzejszej wizycie w Barber Shop Praga ✂️\n\n"
            f"Data: {data}\n"
            f"Godzina: {godz}\n"
            f"Usluga: {usluga}"
            f"{barber_info}\n\n"
            f"Do zobaczenia! Jezeli chcesz odwolac wizyte napisz do nas."
        )

        send_messenger_message_tagged(messenger_id, reminder_text)
        logger.info(f"Wyslano przypomnienie do {messenger_id}")


def send_review_requests():
    """Co 30 minut sprawdza wizyty ktore zakonczyly sie 30 min temu i wysyla prosbe o opinie."""
    logger.info("Sprawdzam zakoncczone wizyty...")
    now = datetime.now(TIMEZONE)
    check_from = (now - timedelta(minutes=40)).isoformat()
    check_to = (now - timedelta(minutes=20)).isoformat()

    events = get_calendar_events(check_from, check_to)
    for event in events:
        summary = event.get("summary", "")
        end_time = event.get("end", "")

        mid_match = re.search(r"mid\.(\d+)", summary)
        if not mid_match:
            continue

        messenger_id = mid_match.group(1)

        # Nie wysylaj jesli juz zbieramy opinie
        if messenger_id in awaiting_review:
            continue

        name_match = re.search(r"Wizyta - ([^-]+) -", summary)
        imie = name_match.group(1).strip() if name_match else "Kliencie"

        parts = summary.split(" - ")
        usluga = parts[2].strip() if len(parts) > 2 else "wizyta"

        barber_match = re.search(r"barber\.([^-]+?)(?:\s*-|$)", summary)
        barber = barber_match.group(1).strip() if barber_match else "barber"

        start_review(messenger_id, imie, usluga, barber)

        review_text = (
            f"Czesc {imie}! Dziekujemy za odwiedziny w Barber Shop Praga ✂️\n\n"
            f"Jak oceniasz dzisiejsza wizyte?\n\n"
            f"Wpisz cyfre od 1 do 5:\n"
            f"1 ⭐ - Slabo\n"
            f"2 ⭐⭐ - Ponizej oczekiwan\n"
            f"3 ⭐⭐⭐ - Srednio\n"
            f"4 ⭐⭐⭐⭐ - Dobrze\n"
            f"5 ⭐⭐⭐⭐⭐ - Swietnie!"
        )

        send_messenger_message_tagged(messenger_id, review_text)
        logger.info(f"Wyslano prosbe o opinie do {messenger_id}")


# ── Narzędzia Claude ──────────────────────────────────────────────────────────

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
                "summary": {"type": "string", "description": "Tytul wizyty"},
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


def handle_tool(name, inputs, sender_id=""):
    if name == "get_calendar_events":
        events = get_calendar_events(inputs["time_min"], inputs["time_max"])
        if not events:
            return "Brak wydarzen w podanym przedziale — termin jest wolny."
        return json.dumps(events, ensure_ascii=False)

    elif name == "create_calendar_event":
        result = create_calendar_event(inputs["summary"], inputs["start"], inputs["end"])
        if "error" in result:
            return "Blad tworzenia rezerwacji: " + result["error"]
        dt = datetime.fromisoformat(inputs["start"])
        data_godz = dt.strftime("%d-%m-%Y, godz. %H:%M")
        msg = "🟢 Nowa rezerwacja!\n" + inputs["summary"] + "\n" + data_godz
        send_telegram_notification(msg)
        return "Rezerwacja utworzona. ID: " + result["id"]

    elif name == "delete_calendar_event":
        ok = delete_calendar_event(inputs["event_id"])
        if ok:
            send_telegram_notification("🔴 Wizyta zostala odwolana przez klienta.")
        return "Wizyta odwolana pomyslnie." if ok else "Nie udalo sie odwolac wizyty."

    return "Nieznane narzedzie: " + name


# ── System Message ────────────────────────────────────────────────────────────

def build_system_message(sender_id=""):
    now = datetime.now(TIMEZONE)
    days = "\n".join(
        (now + timedelta(days=i)).strftime("%A %d %B %Y")
        for i in range(36)
    )
    barbers_list = ", ".join(BARBERS)
    return (
        "Jestes asystentem Barber Shop Praga. Mow po polsku.\n\n"
        "SALON:\n"
        "Adres: ul. Zabkowska 38, Warszawa | Tel: 739-299-091\n"
        "Pon-Pt: 8-21 | Sob: 8-18 | Niedz: 9-15 (wybrane)\n\n"
        f"BARBERZY: {barbers_list}\n\n"
        "CENNIK:\n"
        "Strzyzenie wlosow: 100 zl (40 min)\n"
        "Strzyzenie dlugich wlosow: 120 zl (60 min)\n"
        "Strzyzenie + broda: 160 zl (80 min)\n"
        "Strzyzenie brody: 80 zl (40 min)\n"
        "Golenie brody: 80 zl (40 min)\n"
        "Strzyzenie dzieci 3-11 lat: 100 zl (40 min)\n"
        "Modelowanie + mycie: 40 zl (10 min)\n"
        "Depilacja nosa/uszu: 30 zl (10 min)\n\n"
        "Dzisiaj i najblizsze 35 dni:\n"
        + days +
        "\n\nREZERWACJA:\n"
        "1. Zbierz: imie i nazwisko, telefon, usluge, dzien i godzine\n"
        "2. Zapytaj ktory barber — do wyboru: " + barbers_list + " (lub 'bez preferencji')\n"
        "3. Uzyj get_calendar_events — sprawdz czy termin wolny\n"
        "4. Powiedz: Rezerwuje: [dzien] [data] o [godzina] - [usluga] - barber [imie]. Czy potwierdzasz?\n"
        "5. Po tak — uzyj create_calendar_event z tytulem:\n"
        f"   Wizyta - [Imie Nazwisko] - [usluga] - tel.[telefon] - barber.[imie barbera] - mid.{sender_id}\n"
        "   Daty zawsze z +02:00, np. 2026-04-17T10:00:00+02:00\n"
        "6. Potwierdz klientowi\n\n"
        "ODWOLANIE:\n"
        "1. Zbierz: imie, telefon, date\n"
        "2. get_calendar_events — znajdz wizyte\n"
        "3. Sprawdz telefon w tytule — jesli pasuje uzyj delete_calendar_event\n\n"
        "WOLNE TERMINY:\n"
        "get_calendar_events na wybrany dzien — pokaz wolne sloty co 40 min\n\n"
        "FORMATOWANIE: Uzywaj emoji, unikaj gwiazdek i myslnikow."
    )


# ── Agent Claude ──────────────────────────────────────────────────────────────

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
            system=build_system_message(sender_id),
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
                    result = handle_tool(block.name, block.input, sender_id)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            history.append({"role": "user", "content": tool_results})
        else:
            return "Przepraszam, wystapil blad."


# ── Messenger ─────────────────────────────────────────────────────────────────

def send_message(recipient_id, text):
    chunks = [text[i:i+1900] for i in range(0, len(text), 1900)]
    for chunk in chunks:
        resp = requests.post(
            "https://graph.facebook.com/v25.0/me/messages",
            params={"access_token": PAGE_ACCESS_TOKEN},
            json={
                "recipient": {"id": recipient_id},
                "messaging_type": "RESPONSE",
                "message": {"text": chunk},
            },
        )
        if resp.status_code != 200:
            logger.error(f"Blad wysylania: {resp.text}")


# ── Endpointy Flask ───────────────────────────────────────────────────────────

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

            # ignoruj echo (wiadomosci wysłane przez bota)
            if messaging.get("message", {}).get("is_echo"):
                continue

            # ignoruj duplikaty
            mid = messaging.get("message", {}).get("mid")
            if mid and mid in processed_messages:
                continue
            if mid:
                processed_messages.add(mid)
                if len(processed_messages) > 1000:
                    processed_messages.clear()

            text = messaging.get("message", {}).get("text", "")
            if not text:
                continue

            logger.info(f"Wiadomosc od {sender_id}: {text}")

            # Sprawdz czy klient jest w trakcie dawania opinii
            review_reply = handle_review_flow(sender_id, text)
            if review_reply:
                send_message(sender_id, review_reply)
                continue

            # Normalny agent AI
            send_message(sender_id, "⏳")
            reply = run_agent(sender_id, text)
            send_message(sender_id, reply)

    return "OK", 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": datetime.now(TIMEZONE).isoformat()})


@app.route("/test-reminders", methods=["GET"])
def test_reminders():
    send_reminders()
    return jsonify({"status": "reminders sent"})


@app.route("/test-reviews", methods=["GET"])
def test_reviews():
    send_review_requests()
    return jsonify({"status": "review requests sent"})


# ── Scheduler ─────────────────────────────────────────────────────────────────

scheduler = BackgroundScheduler(timezone=TIMEZONE)
scheduler.add_job(send_reminders, "cron", hour=21, minute=0)
scheduler.add_job(send_review_requests, "interval", minutes=30)
scheduler.start()
logger.info("Scheduler uruchomiony.")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
