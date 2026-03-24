import os
import ssl
import time
import urllib3
import requests
import unicodedata
import pytz
import smtplib
from email.mime.text import MIMEText
import email.utils
from datetime import datetime, timedelta, timezone
from collections import Counter
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
import json

# --- SSL & SYSTEMOVY BYPASS ---
os.environ['WDM_SSL_VERIFY'] = '0'
ssl._create_default_https_context = ssl._create_unverified_context
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- CASOVE PASMO ---
PRG_TZ = pytz.timezone('Europe/Prague')

# --- KONFIGURACE TERMINU ---
MIN_HOUR = 17
MAX_HOUR = 21
MAX_COURTS = 12

MY_DATES = []
try:
    with open("config.json", "r", encoding="utf-8") as f:
        config = json.load(f)
        for d_str in config.get("dates", []):
            MY_DATES.append(datetime.strptime(d_str, "%Y-%m-%d"))
except Exception as e:
    print(f"[VAROVANI] Nepodarilo se nacist config.json: {e}")

# --- PRIJEMCI (Nacitaji se z GitHub Secrets a Env proměnných) ---
RECIPIENTS = [
    (os.getenv("WA_PHONE"), os.getenv("WA_API_KEY")),
    (os.getenv("WA_PHONE_2"), os.getenv("WA_API_KEY_2")),
]

EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")

# Nacteni seznamu emailu z env promenne (ocekava carkou oddelene maily)
_emails_env = os.getenv("EMAIL_LIST", "")
EMAILS = [e.strip() for e in _emails_env.split(",") if e.strip()]

DAYS_MAP = {0: "Po", 1: "Ut", 2: "St", 3: "Ct", 4: "Pa", 5: "So", 6: "Ne"}

def remove_diacritics(text):
    normalized = unicodedata.normalize('NFKD', text)
    return "".join([c for c in normalized if not unicodedata.combining(c)])

def ticks_to_prg_datetime(ticks):
    """Prevede ticks z webu primo na prazsky cas."""
    dt_utc = datetime(1, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ticks // 10)
    return dt_utc.astimezone(PRG_TZ)

def send_whatsapp(message):
    clean_msg = remove_diacritics(message)
    for phone, api_key in RECIPIENTS:
        if not phone or not api_key or "XXXXXX" in api_key:
            continue
        url = f"https://api.callmebot.com/whatsapp.php?phone={phone}&text={requests.utils.quote(clean_msg)}&apikey={api_key}"
        try:
            response = requests.get(url, timeout=10, verify=False)
            if response.status_code == 200:
                print(f"[OK] WhatsApp zprava odeslana na {phone[:6]}***")
            else:
                print(f"[CHYBA] CallMeBot API vratilo status {response.status_code} pro {phone}")
        except Exception as e:
            print(f"[CHYBA] Selhalo odesilani WhatsApp pro {phone}: {e}")

def send_email(message):
    if not EMAIL_USER or not EMAIL_PASS:
        print("[INFO] Nejsou nastavene udaje pro E-mail (EMAIL_USER / EMAIL_PASS).")
        return
    if not EMAILS:
        return

    try:
        msg = MIMEText(message, 'plain', 'utf-8')
        msg['Subject'] = '🏸 Badminton Bot - Aktualizace terminu'
        msg['From'] = EMAIL_USER
        # Pouzivame Bcc (skryta kopie), aby hraci nevideli emaily ostatnich
        msg['Bcc'] = ", ".join(EMAILS)

        # --- LOGIKA PRO SHLUKOVANI DO VLAKNA ---
        msg_id_file = "last_email_id.txt"
        old_msg_id = None
        if os.path.exists(msg_id_file):
            with open(msg_id_file, "r", encoding="utf-8") as f:
                old_msg_id = f.read().strip()

        # Vygenerujeme nove unikatni ID pro tento e-mail
        new_msg_id = email.utils.make_msgid(domain="badmintonbot.local")
        msg['Message-ID'] = new_msg_id

        # Pokud mame stare ID, odkazeme na nej, cimz vznikne vlakno (Thread)
        if old_msg_id:
            msg['In-Reply-To'] = old_msg_id
            msg['References'] = old_msg_id
        # ---------------------------------------

        # Pouzivame Gmail SMTP jako default
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(EMAIL_USER, EMAIL_PASS)
        server.send_message(msg)
        server.quit()

        # Ulozeni noveho ID do souboru pro dalsi beh bota
        with open(msg_id_file, "w", encoding="utf-8") as f:
            f.write(new_msg_id)

        print(f"[OK] E-mail uspesne odeslan na {len(EMAILS)} adres (Vlakno: {new_msg_id}).")
    except Exception as e:
        print(f"[CHYBA] Selhalo odesilani E-mailu: {e}")

def scan_current_day(driver, wait, target_date):
    day_str_short = target_date.strftime('%d.%m.')
    day_name = DAYS_MAP[target_date.weekday()]
    header_id = "ctl00_PageContent_Scheduler_containerBlock_0"
    layer_id = "ctl00_PageContent_Scheduler_containerBlock_verticalContainerappointmentLayer"

    try:
        # Cekame, az se AJAXem nacte spravne datum v hlavicce
        wait.until(EC.text_to_be_present_in_element((By.ID, header_id), day_str_short))
        time.sleep(1) # Kratka pauza pro jistotu dokresleni elementu

        wait.until(EC.presence_of_element_located((By.ID, layer_id)))
        appointments = driver.find_element(By.ID, layer_id).find_elements(By.CLASS_NAME, "dxscApt")

        slots_counter = Counter()
        for apt in appointments:
            try:
                s_ticks = int(apt.get_attribute("data-start-time-utc"))
                e_ticks = int(apt.get_attribute("data-end-time-utc"))
                curr = ticks_to_prg_datetime(s_ticks)
                end = ticks_to_prg_datetime(e_ticks)

                # Zapis obsazenosti po 30 min blocich
                while curr < end:
                    slots_counter[curr] += 1
                    curr += timedelta(minutes=30)
            except Exception as e_apt:
                # Preskocime konkretni rozbity element, ale neukoncujeme celou funkci
                continue

        times_in_day = []

        # Nastaveni limitu v prazskem case (od MIN_HOUR do MAX_HOUR)
        check_time = PRG_TZ.localize(target_date.replace(hour=MIN_HOUR, minute=0, second=0))
        end_limit = PRG_TZ.localize(target_date.replace(hour=MAX_HOUR, minute=0, second=0))

        # Hledame hodinove bloky (2 po sobe jdouci 30min sloty)
        while check_time < end_limit:
            next_time = check_time + timedelta(minutes=30)
            if slots_counter[check_time] < MAX_COURTS and slots_counter[next_time] < MAX_COURTS:
                times_in_day.append(check_time.strftime('%H:%M'))
            check_time += timedelta(minutes=30)

        if times_in_day:
            result = f"{day_name} {day_str_short} | " + ", ".join(times_in_day)
            print(f"  -> Nalezeny terminy: {result}")
            return result

        print(f"  -> Zadny volny termin v case {MIN_HOUR}:00 - {MAX_HOUR}:00.")
        return None

    except Exception as e:
        print(f"[CHYBA] Pri skenovani dne {day_str_short} doslo k chybe: {e}")
        # Vracime None, aby chyba na jednom dni neshodila cely skript
        return None

def run_checker():
    print(f"--- Start kontroly: {datetime.now(PRG_TZ).strftime('%Y-%m-%d %H:%M:%S')} ---")

    options = Options()
    options.add_argument("--headless=new") # Moderni headless zapis
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    # Zrychleni nacteni - ignorujeme obrazky
    options.add_argument("--blink-settings=imagesEnabled=false")

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    wait = WebDriverWait(driver, 15)

    try:
        print("Nacitam stranku rezku...")
        driver.get("https://memberzone.cz/infinit_step_sportcentrum/Scheduler.aspx")

        # Proklikani do spravneho zobrazeni
        wait.until(EC.element_to_be_clickable((By.ID, "HeaderPanel_MyMenu_DXI2_T"))).click()
        time.sleep(2)
        wait.until(EC.element_to_be_clickable((By.ID, "LeftPanel_LeftPanelContent_RadioButtonsGroupBox_ASPxButton3_CD"))).click()
        time.sleep(2)

        report_lines = []
        now_prg = datetime.now(PRG_TZ)

        for target_date in MY_DATES:
            # Kontrola, zda den jiz neprosel (vcetne dneska, pokud je po 21:00)
            if target_date.date() < now_prg.date():
                continue
            if target_date.date() == now_prg.date() and now_prg.hour >= MAX_HOUR:
                continue

            print(f"Proveruji: {target_date.strftime('%d.%m.')}")

            # Skok na datum pomoci JS DevExpress API
            js_goto = f"ASPxClientControl.GetControlCollection().GetByName('ctl00_PageContent_Scheduler').GotoDate(new Date({target_date.year}, {target_date.month - 1}, {target_date.day}));"
            driver.execute_script(js_goto)

            line = scan_current_day(driver, wait, target_date)
            if line:
                report_lines.append(line)

        # Vyhodnoceni reportu
        new_report = "\n".join(report_lines).strip()
        cache_file = "last_report.txt"
        old_report = ""

        if os.path.exists(cache_file):
            with open(cache_file, "r", encoding="utf-8") as f:
                old_report = f.read().strip()

        print("\n--- VYHODNOCENI ---")
        if new_report == old_report:
            print("Zadna zmena v terminech oproti minule kontrole.")
        else:
            if new_report:
                print("ZMENA: Byly nalezeny nove terminy! Odesilam upozorneni.")
                msg = "*🏸 BADMINTON - NOVE TERMINY:* \n\n" + new_report
                send_whatsapp(msg)
                send_email(msg)
            elif old_report:
                print("ZMENA: Vsechny sledovane terminy zmyzely (jsou obsazene). Odesilam upozorneni.")
                msg = "*🏸 BADMINTON:* Vsechny sledovane terminy jsou jiz obsazene."
                send_whatsapp(msg)
                send_email(msg)

            # Ulozeni noveho stavu do cache az po uspesnem odeslani
            with open(cache_file, "w", encoding="utf-8") as f:
                f.write(new_report)

    except Exception as e:
        print(f"[KRITICKA CHYBA] Hlavni smycka selhala: {e}")
    finally:
        print("Ukoncuji WebDriver.")
        driver.quit()

if __name__ == "__main__":
    run_checker()