# Deploying on Render (data that survives restarts)

Render's free web service wipes its local files on every restart, redeploy and
15-minute idle spin-down. This version stores everything in a database instead.

## 1. Files in the GitHub repo
`app.py`, `db.py`, `documents.py`, `requirements.txt`, `templates/`, `static/`
(your pictures go in `static/`). Do NOT commit `hotel.db`. The old
`hotel_records.csv` and the old single-file version are no longer used.

## 2. Create a database
Pick one:
* Render Postgres (Dashboard > New > Postgres). The free one expires after 30
  days, so for a long-lived demo use a paid instance or a free hosted Postgres
  from another provider (check its current limits).
* Copy the database's **Internal** (same region) or **External** connection URL.

## 3. Web service settings
* Build command:  `pip install -r requirements.txt`
* Start command:  `gunicorn app:app`
* Environment variables:

| Name | Value |
|---|---|
| `DATABASE_URL` | the Postgres connection URL from step 2 |
| `SECRET_KEY` | any long random text (keeps logins and links safe) |
| `ADMIN_PASSWORD` | a strong password for the first `admin` account |
| `HOTEL_NAME`, `CURRENCY` | optional (for example `Grand Oasis Hotel`, `SAR`) |

On first start the tables are created automatically, with two accounts:
`admin` (your ADMIN_PASSWORD, or admin123) and `reception` / `hotel123`.
Log in and change the passwords on the Account page, and delete or change
`reception` on the Staff page.

## 4. Real emails (optional)
Without these settings, emails and SMS are saved on the Messages page (demo mode).

| Name | Example |
|---|---|
| `SMTP_HOST` | your email provider's SMTP server |
| `SMTP_PORT` | `587` (or `2525`, see note) |
| `SMTP_USER` / `SMTP_PASSWORD` | your SMTP login or app password |
| `SMTP_SENDER` | the "from" address your provider allows |

Important: Render's free web services block outbound ports 25, 465 and 587.
Use an email provider that offers SMTP on another port (many offer 2525; check
their documentation), or upgrade the web service.

## 5. Run on your own computer
```
pip install -r requirements.txt
python app.py
```
This uses a local file database (`hotel.db`) when `DATABASE_URL` is not set.
