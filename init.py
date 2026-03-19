import os
import ssl
import time
import urllib3
import requests
import unicodedata
from datetime import datetime, timedelta, timezone
from collections import Counter
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# --- SSL & SYSTEMOVY BYPASS ---
os.environ['WDM_SSL_VERIFY'] = '0'
ssl._create_default_https_context = ssl._create_unverified_context
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- KONFIGURACE TERMINU ---
MIN_HOUR = 17
MAX_HOUR = 21
MAX_COURTS = 12

# Dny, ktere chces hlidat (RRRR, M, D)
MY_DATES = [
    datetime(2026, 3, 31),
    datetime(2026, 4, 1),
    datetime(2026, 4, 2),
    datetime(2026, 4, 9),
    datetime(2026, 4, 14)
]

# --- PRIJEMCI (Nacitaji se z GitHub Secrets) ---
RECIPIENTS = [
    (os.getenv("WA_PHONE"), os.getenv("WA_API_KEY")),
    (os.getenv("WA_PHONE_2"), os.getenv("WA_API_KEY_2")),
]

DAYS_MAP = {0: "Po", 1: "Ut", 2: "St", 3: "Ct", 4: "Pa", 5: "So", 6: "Ne"}

def remove_diacritics(text):
    """Odstrani hacky a carky z textu."""
    normalized = unicodedata.normalize('NFKD', text)
    return "".join([c for c in normalized if not unicodedata.combining(c)])

def ticks_to_local_datetime(ticks):
    dt_utc = datetime(1, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ticks // 10)
    return dt_utc.astimezone()

def send_whatsapp(message):
    clean_msg = remove_diacritics(message)

    for phone, api_key in RECIPIENTS:
        if not phone or not api_key or "XXXXXX" in api_key:
            continue
        url = f"https://api.callmebot.com/whatsapp.php?phone={phone}&text={requests.utils.quote(clean_msg)}&apikey={api_key}"
        try:
            requests.get(url, timeout=10, verify=False)
            print(f"[OK] Odeslano na {phone[:6]}***")
        except Exception as e:
            print(f"[CHYBA] Selhalo odesilani pro {phone}: {e}")

def scan_current_day(driver, wait, target_date):
    day_str_short = target_date.strftime('%d.%m.')
    day_name = DAYS_MAP[target_date.weekday()]
    header_id = "ctl00_PageContent_Scheduler_containerBlock_0"
    layer_id = "ctl00_PageContent_Scheduler_containerBlock_verticalContainerappointmentLayer"

    try:
        wait.until(EC.text_to_be_present_in_element((By.ID, header_id), day_str_short))
        time.sleep(2)

        wait.until(EC.presence_of_element_located((By.ID, layer_id)))
        appointments = driver.find_element(By.ID, layer_id).find_elements(By.CLASS_NAME, "dxscApt")

        slots_counter = Counter()
        for apt in appointments:
            try:
                s_ticks = int(apt.get_attribute("data-start-time-utc"))
                e_ticks = int(apt.get_attribute("data-end-time-utc"))
                curr = ticks_to_local_datetime(s_ticks)
                end = ticks_to_local_datetime(e_ticks)
                while curr < end:
                    slots_counter[curr] += 1
                    curr += timedelta(minutes=30)
            except: continue

        times_in_day = []
        check_time = target_date.replace(hour=MIN_HOUR, minute=0, second=0, microsecond=0).astimezone()
        end_limit = target_date.replace(hour=MAX_HOUR, minute=0, second=0, microsecond=0).astimezone()

        while check_time < end_limit:
            next_time = check_time + timedelta(minutes=30)
            if slots_counter[check_time] < MAX_COURTS and slots_counter[next_time] < MAX_COURTS:
                times_in_day.append(check_time.strftime('%H:%M'))
            check_time += timedelta(minutes=30)

        if times_in_day:
            return f"{day_name} {day_str_short} | " + ", ".join(times_in_day)
        return None
    except:
        return None

def run_checker():
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    wait = WebDriverWait(driver, 20)

    try:
        driver.get("https://memberzone.cz/infinit_step_sportcentrum/Scheduler.aspx")
        wait.until(EC.element_to_be_clickable((By.ID, "HeaderPanel_MyMenu_DXI2_T"))).click()
        time.sleep(2)
        wait.until(EC.element_to_be_clickable((By.ID, "LeftPanel_LeftPanelContent_RadioButtonsGroupBox_ASPxButton3_CD"))).click()
        time.sleep(2)

        report_lines = []
        for target_date in MY_DATES:
            if target_date.date() < datetime.now().date():
                continue

            print(f"Proveruji datum: {target_date.strftime('%d.%m.')}")
            js_goto = f"ASPxClientControl.GetControlCollection().GetByName('ctl00_PageContent_Scheduler').GotoDate(new Date({target_date.year}, {target_date.month - 1}, {target_date.day}));"
            driver.execute_script(js_goto)

            line = scan_current_day(driver, wait, target_date)
            if line: report_lines.append(line)

        new_report = "\n".join(report_lines) if report_lines else ""
        cache_file = "last_report.txt"
        old_report = ""

        if os.path.exists(cache_file):
            with open(cache_file, "r") as f: old_report = f.read()

        if new_report == old_report:
            print("Zadna zmena. Zprava se neposila.")
        else:
            if new_report:
                msg = "*BADMINTON - NOVE TERMINY:* \n\n" + new_report
                send_whatsapp(msg)
            elif old_report:
                send_whatsapp("*BADMINTON:* Vsechny sledovane terminy jsou jiz obsazene.")

            with open(cache_file, "w") as f: f.write(new_report)

    finally:
        driver.quit()

if __name__ == "__main__":
    run_checker()