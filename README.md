# Road Rescue - On Road Assistance System

Fast Help. Safe Journey.
Flask (Python) + MySQL (XAMPP / phpMyAdmin) + HTML/CSS/JavaScript.

## Quick start (Windows + XAMPP + VS Code)

1. Open **XAMPP Control Panel** and click **Start** for **Apache** and **MySQL**.
2. Unzip this folder and open it in **VS Code** (File > Open Folder).
3. Open a terminal in VS Code and run:

   ```
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   python app.py
   ```
4. Open **http://127.0.0.1:5000** in Google Chrome.

The first run creates the `road_rescue` database, all tables, the 5 services,
the admin account and a demo provider automatically.
Prefer phpMyAdmin? Open http://localhost/phpmyadmin > Import > choose `database/schema.sql`.
(You still run `python app.py` once to create the admin account.)

If your MySQL root user has a password, change `DB_PASSWORD` in `config.py` (the only place DB settings live).

## Logins

| Role | Email | Password |
|------|-------|----------|
| Admin | admin@roadrescue.com | Admin@123 |
| Service provider (demo) | provider@roadrescue.com | Provider@123 |
| Customer | register a new account | - |

The login page has three tabs (Customer / Provider / Admin). Each role gets its own dashboard and cannot open the others.

## Dashboards

* **User:** `/user/dashboard` - requests, history, tracking, vehicles, feedback, profile
* **Provider:** `/provider/dashboard` - assigned jobs, customer/vehicle/location, status updates, completed history
* **Admin:** `/admin/dashboard` plus separate pages for Users, Service Providers, Services, **Assistance Requests**, Assignments, **Feedback**, **Database Manager**, Reports

## Request workflow

Pending > Assigned (admin) > Accepted > On the Way > In Progress > Completed (provider) > Feedback (user). Users may cancel while Pending/Assigned.

## Security

Passwords hashed (Werkzeug), session login, role checks on every route, CSRF tokens on all forms, users only see their own data, parameterised SQL.

## Troubleshooting

* "Could not connect to MySQL" - start MySQL in XAMPP; check `config.py`; make sure nothing else uses port 3306.
* `pip` not found - install Python 3.9+ and tick "Add Python to PATH".
* Icons/fonts missing - they load from CDNs, so an internet connection is needed.
