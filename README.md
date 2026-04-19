# Barber Shop AI Bot — bez n8n

Bot na Messengera z Claude AI i Google Calendar. Zero n8n, zero miesięcznych opłat za platformę.

## Koszt miesięczny

| Usługa | Koszt |
|--------|-------|
| Render.com (hosting) | **0 zł** (darmowy plan) |
| Claude API | ~10-30 zł |
| Google Calendar | **0 zł** |
| **Razem** | **~10-30 zł/mies.** |

Zamiast 100+ zł za n8n.cloud.

---

## Pliki projektu

```
messenger-bot/
├── app.py              ← główny kod bota
├── requirements.txt    ← zależności Python
├── .env.example        ← szablon zmiennych
└── credentials.json    ← plik Google (pobierz z Google Cloud)
```

---

## Krok 1 — Google Calendar API

1. Wejdź na **console.cloud.google.com**
2. Utwórz projekt → włącz **Google Calendar API**
3. Utwórz dane uwierzytelniające → **OAuth 2.0** → Desktop App
4. Pobierz JSON i zapisz jako `credentials.json`

---

## Krok 2 — Wgraj na Render.com

1. Załóż konto na **render.com**
2. Kliknij **"New"** → **"Web Service"**
3. Połącz z GitHub (wgraj pliki do repozytorium)
4. Ustaw zmienne środowiskowe (Environment Variables):
   - `FB_PAGE_ACCESS_TOKEN` — token strony z Meta Developers
   - `FB_APP_SECRET` — App Secret z Meta Developers
   - `FB_VERIFY_TOKEN` — `barbershop2026`
   - `ANTHROPIC_API_KEY` — klucz z console.anthropic.com
5. Start Command: `gunicorn app:app`
6. Kliknij **Deploy**

Po wdrożeniu dostaniesz URL np. `https://twoj-bot.onrender.com`

---

## Krok 3 — Autoryzacja Google Calendar

Przy pierwszym uruchomieniu musisz autoryzować dostęp do kalendarza:

1. Pobierz projekt lokalnie na komputer
2. Zainstaluj Python i uruchom: `python app.py`
3. Otworzy się przeglądarka — zaloguj się i zatwierdź
4. Zostanie utworzony plik `token_calendar.pickle`
5. Wgraj ten plik do Render.com przez panel lub GitHub

---

## Krok 4 — Podepnij do Meta Developers

1. Wejdź na **developers.facebook.com**
2. Twoja aplikacja → **Przykłady użycia** → **Customize**
3. W **Configure webhooks** wpisz:
   - Callback URL: `https://twoj-bot.onrender.com/webhook`
   - Verify Token: `barbershop2026`
4. Kliknij **Verify and Save**
5. Dodaj subskrypcje: **messages**, **messaging_postbacks**

---

## Różnica vs n8n

| | n8n.cloud | Ten kod |
|--|-----------|---------|
| Koszt | ~100 zł/mies. | ~15 zł/mies. |
| Konfiguracja | klocki wizualne | plik Python |
| Elastyczność | ograniczona | pełna |
| Aktualizacje | przez n8n | przez kod |
