import logging
import signal
from datetime import datetime, timedelta
from pytz import timezone
from quart import Quart, jsonify, render_template, request, Response
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from playwright.async_api import async_playwright, Page, BrowserContext, TimeoutError
import os
import re
import requests
import csv
import json
import io
import psutil
import asyncio
from threading import Lock
from typing import Optional, Dict, List
import pyotp
import random
from logging.handlers import RotatingFileHandler
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, 
                                Table, TableStyle)
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4

#######################################################################
#                            CONFIG & CONSTANTS
#######################################################################

LOCAL_TIMEZONE = timezone('Europe/London')  # Update to your local timezone

# Load configuration
with open('config.json', 'r') as config_file:
    config = json.load(config_file)

DEBUG_MODE = config.get('debug', False) 
FORM_URL = config['form_url']
LOGIN_URL = config['login_url']
CHAT_WEBHOOK_URL = config['chat_webhook_url']
SECRET_KEY = config['secret_key']
LOGO_URL = "https://your-logo-url.com/logo.png"  # Update with your logo

# File paths
SCHEDULE_FILE = 'schedules.json'
LOG_FILE = os.path.join('output', 'submissions.log')
STORAGE_STATE = 'state.json'
os.makedirs('output', exist_ok=True)  # Ensure output directory exists

# Constants related to concurrency and retries
MAX_TOTAL_RETRIES = 5
MAX_HEADLESS_TIMEOUT = 15000  # For page navigations, can adjust as needed

#######################################################################
#                             APP SETUP & LOGGING
#######################################################################

app = Quart(__name__)
app.secret_key = SECRET_KEY

def setup_logging():
    app_logger = logging.getLogger('app')
    app_logger.setLevel(logging.INFO)

    app_file_handler = RotatingFileHandler('app.log', maxBytes=10**7, backupCount=5)
    console_handler = logging.StreamHandler()

    app_file_handler.setLevel(logging.INFO)
    console_handler.setLevel(logging.INFO)

    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    app_file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    app_logger.addHandler(app_file_handler)
    app_logger.addHandler(console_handler)

    # Suppress some internal logs for clarity
    quart_app_logger = logging.getLogger('quart.app')
    quart_app_logger.setLevel(logging.WARNING)
    logging.getLogger('hypercorn.access').disabled = True
    quart_serving_logger = logging.getLogger('quart.serving')
    quart_serving_logger.setLevel(logging.WARNING)

    return app_logger

app_logger = setup_logging()

# Suppress Werkzeug logs if any
werkzeug_logger = logging.getLogger('werkzeug')
werkzeug_logger.setLevel(logging.ERROR)

#######################################################################
#                          GLOBAL VARIABLES & LOCKS
#######################################################################

log_lock = Lock()
progress_lock = Lock()

# Runtime states and counters
urls_data = []
stop_flag = False
pause_flag = False
login_required = False
otp_required = False
is_logged_in = False
progress = {
    "current": 0, 
    "total": 0, 
    "currentUrl": "N/A", 
    "lastStore": "N/A", 
    "lastUpdate": "N/A"
}
otp = None
last_run_time = None
next_run_time = None
start_time = None

retry_urls = []
final_failed_urls = []
url_processing_times = []
form_filling_times = []
retry_count = 0
form_errors = 0
successful_submissions = 0
failed_submissions = 0
url_retry_counts = {}
cache = {}
run_failures = []
successful_retries = []

shutdown_event = asyncio.Event()

# Store selector for extraction
STORE_SELECTOR_XPATH = '//*[@id="content"]/div/div[2]/div[2]/kat-tabs/kat-tab[1]/div/div[2]/kat-table/kat-table-body/kat-table-row[1]/kat-table-cell[2]'

#######################################################################
#                      SIGNAL HANDLER & SHUTDOWN LOGIC
#######################################################################

def handle_shutdown_signal():
    app_logger.info("Shutdown signal received. Initiating graceful shutdown...")
    shutdown_event.set()

for sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, lambda s, f: handle_shutdown_signal())

async def shutdown():
    """Handle shutdown: stop tasks and scheduler."""
    app_logger.info("Shutdown initiated. Waiting for ongoing tasks to finish...")
    shutdown_event.set()
    await asyncio.sleep(2)  # give tasks a moment
    global scheduler
    if scheduler:
        scheduler.shutdown(wait=False)
    app_logger.info("Scheduler shut down successfully.")

#######################################################################
#                           UTILITY FUNCTIONS
#######################################################################

def calculate_exponential_backoff(attempt: int, base_delay: float = 1.0, max_delay: float = 300.0) -> float:
    """Calculate exponential backoff with jitter."""
    exponential_delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
    jitter = random.uniform(0, exponential_delay)
    return jitter

def load_default_data():
    """Load URLs from a default CSV file."""
    global urls_data
    try:
        with open('urls.csv', 'r') as file:
            urls_data = [row.strip() for row in file.readlines()]
        app_logger.info(f'{len(urls_data)} URLs loaded from urls.csv')
    except Exception as e:
        app_logger.error("Failed to load the default CSV file.", exc_info=True)

def ensure_storage_state():
    """Ensure that the storage state file for Playwright exists and is valid."""
    if not os.path.exists(STORAGE_STATE) or os.path.getsize(STORAGE_STATE) == 0:
        with open(STORAGE_STATE, 'w') as f:
            json.dump({}, f)
        app_logger.warning(f"{STORAGE_STATE} does not exist or is empty. A new login will be performed.")
    else:
        try:
            with open(STORAGE_STATE, 'r') as f:
                data = json.load(f)
                if not data:
                    app_logger.warning(f"{STORAGE_STATE} is empty. A new login will be performed.")
        except json.JSONDecodeError:
            app_logger.warning(f"{STORAGE_STATE} is corrupted. Resetting storage state.")
            with open(STORAGE_STATE, 'w') as f:
                json.dump({}, f)

def update_last_run_time():
    global last_run_time
    last_run_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def update_next_run_time():
    global next_run_time
    if scheduler:
        jobs = scheduler.get_jobs()
        if jobs:
            job = jobs[0]
            next_run_time_dt = job.next_run_time
            if next_run_time_dt:
                next_run_time = next_run_time_dt.strftime('%Y-%m-%d %H:%M:%S')
            else:
                next_run_time = None
        else:
            next_run_time = None
    else:
        next_run_time = None

def send_chat_message(message: str):
    """Send a plain text message to Google Chat."""
    headers = {"Content-Type": "application/json; charset=UTF-8"}
    data = {"text": message}
    try:
        response = requests.post(CHAT_WEBHOOK_URL, headers=headers, json=data)
        if response.status_code != 200:
            app_logger.error(f"Failed to send message to Google Chat: {response.text}")
    except Exception as e:
        app_logger.error(f"Exception sending message to Google Chat: {str(e)}", exc_info=True)

def send_general_failure_message(issue: str):
    """Send a general failure message to Google Chat."""
    headers = {"Content-Type": "application/json; charset=UTF-8"}
    data = {"text": f"⚠️ **Issue Detected:** {issue}"}
    try:
        response = requests.post(CHAT_WEBHOOK_URL, headers=headers, json=data)
        if response.status_code != 200:
            app_logger.error(f"Failed to send failure message: {response.text}")
    except Exception as e:
        app_logger.error(f"Exception sending failure message: {str(e)}", exc_info=True)

#######################################################################
#                             AUTH & LOGIN
#######################################################################

async def is_login_needed(test_url: str) -> bool:
    """Check if login is required by navigating to a test URL with stored state."""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=not DEBUG_MODE, slow_mo=50 if DEBUG_MODE else 0)
            context = await browser.new_context(storage_state=STORAGE_STATE)
            page = await context.new_page()
            await page.goto(test_url)
            login_redirect = ("signin" in page.url or "ap/signin" in page.url)
            await browser.close()
            return login_redirect
    except Exception as e:
        app_logger.error("Failed to check login status.", exc_info=True)
        await send_general_failure_message("Failed to check login status.")
        return True

async def perform_login_and_otp(page: Page) -> bool:
    """Perform login and handle OTP if required."""
    global login_required, otp_required, is_logged_in
    try:
        await page.goto(LOGIN_URL)
        email_field = await page.query_selector('input[id="ap_email"]')

        if email_field:
            await page.fill('input[id="ap_email"]', config['login_email'])
            continue_button = await page.query_selector('input[id="continue"]')
            if continue_button:
                await continue_button.click()
                await asyncio.sleep(2)

        password_field = await page.query_selector('input[id="ap_password"]')
        if password_field:
            await page.fill('input[id="ap_password"]', config['login_password'])
            async with page.expect_navigation():
                await page.click('input[id="signInSubmit"]')
        else:
            issue = "Password field not found during login."
            run_failures.append(issue)
            return False

        # Wait for OTP if needed
        try:
            otp_field = await page.wait_for_selector('input[type="tel"]', timeout=15000)
            if otp_field:
                # Generate TOTP
                try:
                    totp = pyotp.TOTP(config['otp_secret_key'])
                    otp_code = totp.now()
                except Exception as e:
                    issue = f"Failed to generate OTP: {e}"
                    run_failures.append(issue)
                    return False
                await page.fill('input[type="tel"]', otp_code)
                dont_require_otp = await page.query_selector("input[name='rememberDevice']")
                if dont_require_otp:
                    await dont_require_otp.set_checked(True)

                sign_in_button = await page.query_selector('input[id="auth-signin-button"]') or \
                                 await page.query_selector('input[id="signInSubmit"]') or \
                                 await page.query_selector('//input[@type="submit"]')

                if sign_in_button:
                    async with page.expect_navigation():
                        await sign_in_button.click()
                else:
                    issue = "Sign In button not found after entering OTP."
                    run_failures.append(issue)
                    return False
            # If no OTP field appears, proceed as normal
        except TimeoutError:
            # No OTP required
            pass

        if "signin" in page.url or "ap/signin" in page.url:
            issue = "Login failed after OTP submission."
            run_failures.append(issue)
            return False

        # Login successful
        login_required = False
        otp_required = False
        is_logged_in = True
        await page.context.storage_state(path=STORAGE_STATE)
        return True

    except Exception as e:
        issue = f"Login/OTP failed: {str(e)}"
        run_failures.append(issue)
        return False

async def login_and_get_context(headless: bool = True) -> Optional[BrowserContext]:
    """Create a browser context and perform login if needed."""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless, slow_mo=50 if not headless else 0)
            context = await browser.new_context()
            page = await context.new_page()
            if await perform_login_and_otp(page):
                await context.storage_state(path=STORAGE_STATE)
                if headless:
                    await browser.close()
                return context
            await browser.close()
    except Exception as e:
        app_logger.error("Failed to create browser context or login.", exc_info=True)
        await send_general_failure_message("Failed to create browser context or login.")
    return None

#######################################################################
#                       URL PROCESSING & EXTRACTION
#######################################################################

async def wait_for_selector_with_retry(page: Page, selector: str, retries: int = 3, base_delay: float = 1.0):
    """Wait for a selector with retries and exponential backoff."""
    global retry_count
    for attempt in range(1, retries + 1):
        if shutdown_event.is_set():
            raise Exception("Shutdown initiated during wait for selector.")
        try:
            return await page.wait_for_selector(selector, timeout=15000)
        except TimeoutError as e:
            app_logger.warning(f"Attempt {attempt} - Timeout waiting for {selector}: {str(e)}")
            retry_count += 1
            if attempt < retries:
                delay = calculate_exponential_backoff(attempt, base_delay=base_delay)
                app_logger.info(f"Retrying in {delay:.2f}s (Attempt {attempt}/{retries})")
                await asyncio.sleep(delay)
            else:
                issue = f"Failed to find selector {selector} after {retries} attempts."
                run_failures.append(issue)
                raise

async def process_url(page: Page, url: str) -> Optional[Dict[str, str]]:
    """Process a single URL to extract required data."""
    global login_required, otp_required, is_logged_in, url_processing_times

    # Check cache
    cached_data = cache.get(url)
    if cached_data:
        cache_time = cached_data.get('timestamp')
        if cache_time and datetime.now() - cache_time < timedelta(minutes=30):
            app_logger.info(f"Using cached data for URL: {url}")
            return cached_data['data']

    start_process_time = datetime.now()
    if stop_flag or shutdown_event.is_set():
        return None

    try:
        await page.goto(url, timeout=MAX_HEADLESS_TIMEOUT)
        await asyncio.sleep(1)

        # Check if redirected to login
        if "signin" in page.url or "ap/signin" in page.url:
            issue = f"Redirected to login for {url}."
            run_failures.append(issue)
            if not is_logged_in:
                context = await login_and_get_context(headless=not DEBUG_MODE)
                if context:
                    # Retry after successful login
                    return await process_url(page, url)
                else:
                    raise Exception("Login failed during URL processing.")
            else:
                # Force re-login if already logged in
                context = await login_and_get_context(headless=not DEBUG_MODE)
                if context:
                    return await process_url(page, url)
                else:
                    raise Exception("Re-login failed.")

        # Extract store name
        store_element = await wait_for_selector_with_retry(page, STORE_SELECTOR_XPATH)
        if not store_element:
            raise Exception("Store element not found.")
        store = await store_element.inner_text()
        await asyncio.sleep(1)

        # Wait for table headers
        last_cell_xpath = '//*[@id="content"]/div/div[2]/div[2]/kat-tabs/kat-tab[1]/div/div[2]/kat-table/kat-table-head/kat-table-row[2]/kat-table-cell[11]/div'
        await wait_for_selector_with_retry(page, last_cell_xpath)

        row_xpath = '//*[@id="content"]/div/div[2]/div[2]/kat-tabs/kat-tab[1]/div/div[2]/kat-table/kat-table-head/kat-table-row[2]'
        row_element = await wait_for_selector_with_retry(page, row_xpath)
        if not row_element:
            raise Exception("Row element not found.")

        # Extract cells
        cells = await row_element.query_selector_all('kat-table-cell')
        row_data = [await cell.inner_text() for cell in cells]

        if len(row_data) < 11:
            raise Exception("Insufficient row data.")

        data = {
            'url': url,
            'store': store,
            'orders': row_data[3] if len(row_data) > 3 else '',
            'units': row_data[4] if len(row_data) > 4 else '',
            'fulfilled': row_data[5] if len(row_data) > 5 else '',
            'uph': row_data[6] if len(row_data) > 6 else '',
            'inf': row_data[7] if len(row_data) > 7 else '',
            'found': row_data[8] if len(row_data) > 8 else '',
            'cancelled': row_data[9] if len(row_data) > 9 else '',
            'lates': row_data[10] if len(row_data) > 10 else '',
            'field_11': row_data[1] if len(row_data) > 1 else '',
            'availability': ''
        }

        # Validate critical fields
        if not data['orders'] or not data['units'] or not data['fulfilled']:
            raise Exception("Critical fields missing in data.")

        # Click on Availability tab
        availability_tab_xpath = '//span[@slot="label" and text()="Availability"]'
        await page.click(availability_tab_xpath)
        availability_table_xpath = '//*[@id="content"]/div/div[2]/div[2]/kat-tabs/kat-tab[4]/div/div[2]/kat-table/kat-table-head/kat-table-row[2]/kat-table-cell[7]/div'
        availability_data = await wait_for_selector_with_retry(page, availability_table_xpath)
        if availability_data:
            data['availability'] = await availability_data.inner_text()

        # Basic validation
        non_blank_data_points = sum(1 for v in data.values() if v.strip())
        if non_blank_data_points <= 4:
            raise Exception("Insufficient data points.")

        url_processing_times.append((datetime.now() - start_process_time).total_seconds())
        cache[url] = {'data': data, 'timestamp': datetime.now()}
        return data

    except Exception as e:
        issue = f"Failed to process URL {url}: {str(e)}"
        run_failures.append(issue)
        raise

#######################################################################
#                          FORM SUBMISSION
#######################################################################

def read_submission_logs():
    """Read existing submission logs from CSV."""
    logs = []
    try:
        with log_lock:
            if os.path.exists(LOG_FILE):
                with open(LOG_FILE, 'r') as csvfile:
                    reader = csv.DictReader(csvfile)
                    logs = list(reader)
    except FileNotFoundError:
        app_logger.error("Submission log file not found.")
    return logs

def log_submission(data: Dict[str, str]):
    """Log a new submission entry."""
    log_file = LOG_FILE
    fieldnames = ['timestamp', 'store', 'orders', 'units', 'fulfilled', 'uph', 'inf', 
                  'found', 'cancelled', 'lates', 'time_available']

    try:
        with log_lock:
            existing_logs = []
            if os.path.exists(log_file):
                with open(log_file, 'r') as csvfile:
                    reader = csv.DictReader(csvfile)
                    existing_logs = list(reader)

            new_log_date = data['timestamp'].split(' ')[0]
            new_store = data['store']

            # Avoid duplicates for the same store/date
            if any(log['timestamp'].split(' ')[0] == new_log_date and log['store'] == new_store for log in existing_logs):
                return

            existing_logs.append(data)
            with open(log_file, 'w', newline='') as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(existing_logs)
    except IOError:
        app_logger.error("I/O error occurred while logging submission.")

async def fill_google_form(page: Page, data: Dict[str, str]):
    """Fill and submit the Google Form for extracted data."""
    global form_filling_times, form_errors, successful_submissions

    start_time_local = datetime.now()
    # Retry navigation to form
    for attempt in range(1, 4):
        if shutdown_event.is_set():
            return
        try:
            await page.goto(FORM_URL, timeout=90000)
            break
        except TimeoutError:
            if attempt < 3:
                delay = calculate_exponential_backoff(attempt)
                await asyncio.sleep(delay)
            else:
                form_errors += 1
                run_failures.append(f"Form navigation failed after 3 attempts for store {data['store']}.")
                return

    try:
        # Field mapping could be abstracted to a config
        field_labels = {
            'url': "Field 1",
            'store': "Field 2",
            'orders': "Field 3",
            'units': "Field 4",
            'fulfilled': "Field 5",
            'uph': "Field 6",
            'inf': "Field 7",
            'found': "Field 8",
            'cancelled': "Field 9",
            'lates': "Field 10",
            'field_11': "Field 11",
            'availability': "Field 12"
        }

        for key, label in field_labels.items():
            if key == 'field_11':
                continue
            try:
                await page.get_by_label(label, exact=True).fill(data[key])
            except Exception as e:
                form_errors += 1
                issue = f"Error filling {label} with {data[key]} for store {data['store']}: {str(e)}"
                run_failures.append(issue)

        try:
            await page.get_by_label("Submit", exact=True).click()
            await page.wait_for_selector("//div[contains(text(),'Your response has been recorded.')]", timeout=30000)
            successful_submissions += 1
        except Exception as e:
            form_errors += 1
            issue = f"Error during form submission for {data['store']}: {str(e)}"
            run_failures.append(issue)

        log_entry = (f"{datetime.now().strftime('%H:%M')} {data['store']} submitted "
                     f"(Orders {data['orders']}, Units {data['units']}, "
                     f"Fulfilled {data['fulfilled']}, UPH {data['uph']}, INF {data['inf']}%, "
                     f"Found {data['found']}%, Cancelled {data['cancelled']}, Lates {data['lates']}%, "
                     f"Time available: {data['availability']})")

        with log_lock:
            app_logger.info(log_entry)
            progress["lastUpdate"] = log_entry

        submission_data = {
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'store': data['store'],
            'orders': data['orders'],
            'units': data['units'],
            'fulfilled': data['fulfilled'],
            'uph': data['uph'],
            'inf': data['inf'],
            'found': data['found'],
            'cancelled': data['cancelled'],
            'lates': data['lates'],
            'time_available': data['availability']
        }
        log_submission(submission_data)
        form_filling_times.append((datetime.now() - start_time_local).total_seconds())

    except Exception as e:
        form_errors += 1
        issue = f"Failed to fill out the Google Form for {data['store']}: {str(e)}"
        run_failures.append(issue)

async def submit_all_forms(current_run_data: List[Dict[str, str]]):
    """Submit all extracted data to the Google Form."""
    global cache, form_filling_times, form_errors, successful_submissions

    if not current_run_data:
        app_logger.info("No new data to submit forms.")
        return

    tasks = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=not DEBUG_MODE, slow_mo=50 if DEBUG_MODE else 0)
            context = await browser.new_context()

            for data in current_run_data:
                page = await context.new_page()
                tasks.append(asyncio.create_task(fill_google_form(page, data)))

            await asyncio.gather(*tasks)
            await browser.close()

        end = datetime.now()
        total_time = (end - start_time).total_seconds() if start_time else 0
        avg_form_time = sum(form_filling_times)/len(form_filling_times) if form_filling_times else 0

        app_logger.info("Form Submission Summary:")
        app_logger.info(f"Total Time Taken: {total_time:.2f} sec")
        app_logger.info(f"Total Successful Submissions: {successful_submissions}")
        app_logger.info(f"Total Form Errors: {form_errors}")
        app_logger.info(f"Average Form Filling Time: {avg_form_time:.2f} sec")

    except Exception as e:
        issue = f"Error during form submission: {str(e)}"
        run_failures.append(issue)

#######################################################################
#                         PDF REPORT GENERATION
#######################################################################

def get_latest_logs_per_store_for_today():
    logs = read_submission_logs()
    latest_logs = {}
    today_date = datetime.now().strftime('%Y-%m-%d')

    for log in logs:
        log_date = log['timestamp'].split(' ')[0]
        if log_date == today_date:
            store = log['store']
            key = (log_date, store)
            if key not in latest_logs or log['timestamp'] > latest_logs[key]['timestamp']:
                latest_logs[key] = log

    entries = []
    for log in latest_logs.values():
        entries.append(
            f"{log['timestamp']} - **{log['store']}** submitted "
            f"(Orders: {log['orders']}, Units: {log['units']}, Fulfilled: {log['fulfilled']}, "
            f"UPH: {log['uph']}, INF: {log['inf']}%, Found: {log['found']}%, "
            f"Cancelled: {log['cancelled']}, Lates: {log['lates']}%, Time Available: {log['time_available']})"
        )
    return "\n".join(entries)

async def generate_pdf_report(total_time, processed_stores, retry_count, failed_submissions, avg_url_processing_time, avg_form_filling_time, failed_urls):
    """Generate a PDF report with the run's summary."""
    try:
        report_datetime = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        pdf_filename = f"Daily_Report_{report_datetime}.pdf"
        pdf_path = os.path.join('output', pdf_filename)

        doc = SimpleDocTemplate(pdf_path, pagesize=A4, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=18)
        Story = []
        styles = getSampleStyleSheet()

        # Title
        Story.append(Paragraph("Daily Processing Report", styles['Title']))
        Story.append(Spacer(1, 12))

        # Date
        report_dt_formatted = datetime.now().strftime('%B %d, %Y %H:%M:%S')
        Story.append(Paragraph(f"Date and Time: {report_dt_formatted}", styles['Normal']))
        Story.append(Spacer(1, 12))

        # Summary Table
        summary_data = [
            ["Total Time Taken (seconds):", f"{total_time:.2f}"],
            ["Total Stores Processed:", f"{processed_stores}"],
            ["Total Retries:", f"{retry_count}"],
            ["Total Failed Submissions:", f"{failed_submissions}"],
            ["Avg URL Processing Time (seconds):", f"{avg_url_processing_time:.2f}"],
            ["Avg Form Filling Time (seconds):", f"{avg_form_filling_time:.2f}"]
        ]
        from reportlab.lib.units import inch
        summary_table = Table(summary_data, hAlign='LEFT', colWidths=[250, 150])
        summary_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ]))
        Story.append(summary_table)
        Story.append(Spacer(1, 24))

        # Failed URLs
        if failed_urls:
            Story.append(Paragraph("Failed URLs", styles['Heading2']))
            failed_urls_data = [["#", "URL"]]
            for idx, url in enumerate(failed_urls, start=1):
                failed_urls_data.append([str(idx), url])

            failed_urls_table = Table(failed_urls_data, hAlign='LEFT', colWidths=[50, 400])
            failed_urls_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.red),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ]))
            Story.append(failed_urls_table)
            Story.append(Spacer(1, 24))

        # Latest Logs
        latest_logs = get_latest_logs_per_store_for_today()
        if latest_logs:
            Story.append(Paragraph("Latest Logs", styles['Heading2']))
            for line in latest_logs.split('\n'):
                Story.append(Paragraph(line, styles['Normal']))
                Story.append(Spacer(1, 6))

        doc.build(Story)
        app_logger.info(f"PDF report generated at {pdf_path}")
    except Exception as e:
        app_logger.error(f"Failed to generate PDF: {str(e)}", exc_info=True)

#######################################################################
#                         DAILY SUMMARY & NOTIFICATIONS
#######################################################################

def send_chat_message_with_card(summary: str, latest_logs: str):
    headers = {"Content-Type": "application/json; charset=UTF-8"}
    data = {
        "cards": [
            {
                "header": {
                    "title": "*Daily Processing Summary*",
                    "imageUrl": LOGO_URL,
                    "imageStyle": "IMAGE"
                },
                "sections": [
                    {
                        "widgets": [
                            {"textParagraph": {"text": summary}}
                        ]
                    },
                    {
                        "header": "Latest Logs",
                        "widgets": [
                            {"textParagraph": {"text": latest_logs}}
                        ]
                    },
                    {
                        "widgets": [
                            {
                                "buttons": [
                                    {
                                        "textButton": {
                                            "text": "View latest logs",
                                            "onClick": {
                                                "openLink": {
                                                    "url": "http://your_server_address:5002/logs"
                                                }
                                            }
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]
    }
    try:
        response = requests.post(CHAT_WEBHOOK_URL, headers=headers, json=data)
        if response.status_code != 200:
            app_logger.error(f"Failed to send message to Google Chat: {response.text}")
    except Exception as e:
        app_logger.error(f"Exception sending message to Google Chat: {str(e)}", exc_info=True)

def send_daily_summary(total_time, processed_stores, retry_count_, failed_submissions_, avg_url_time, avg_form_time):
    """Send a formatted daily summary to Google Chat."""
    summary_message = (
        f"**Daily Processing Summary:**\n"
        f"• Total Time: {total_time:.2f}s\n"
        f"• Stores Processed: {processed_stores}\n"
        f"• Retries: {retry_count_}\n"
        f"• Failed Submissions: {failed_submissions_}\n"
        f"• Avg URL Processing: {avg_url_time:.2f}s\n"
        f"• Avg Form Filling: {avg_form_time:.2f}s\n"
    )
    latest_logs = get_latest_logs_per_store_for_today()
    send_chat_message_with_card(summary_message, latest_logs)

def send_failed_urls(final_failed_urls):
    """Notify about final failed URLs."""
    if final_failed_urls:
        failed_urls_message = "### ❌ **Failed URLs After All Retries:**\n" + "\n".join([f"- {url}" for url in final_failed_urls])
        send_chat_message(failed_urls_message)

#######################################################################
#                           CONCURRENCY CONTROL
#######################################################################

class AsyncSemaphore:
    """A semaphore to control concurrency and adjust at runtime."""
    def __init__(self, initial: int):
        self._semaphore = asyncio.Semaphore(initial)
        self._lock = asyncio.Lock()
        self._current = initial

    async def acquire(self):
        await self._semaphore.acquire()

    def release(self):
        self._semaphore.release()

    async def adjust(self, new_limit: int):
        async with self._lock:
            if new_limit > self._current:
                # Increase concurrency
                for _ in range(new_limit - self._current):
                    self._semaphore.release()
            elif new_limit < self._current:
                # Decrease concurrency
                for _ in range(self._current - new_limit):
                    await self._semaphore.acquire()
            self._current = new_limit
            app_logger.info(f"Semaphore adjusted to {self._current}")

    def get_limit(self) -> int:
        return self._current

async def adjust_concurrency(semaphore: AsyncSemaphore):
    """Adjust concurrency based on system load every 10 seconds."""
    max_concurrency = config['max_concurrency']
    min_concurrency = config['min_concurrency']

    try:
        while True:
            if shutdown_event.is_set():
                break
            cpu_usage = psutil.cpu_percent(interval=None)
            memory_info = psutil.virtual_memory()
            current_limit = semaphore.get_limit()
            new_limit = current_limit

            if cpu_usage > 99 or memory_info.percent > 90:
                if current_limit > min_concurrency:
                    new_limit = max(min_concurrency, current_limit - 1)
                    app_logger.info(f"High load (CPU:{cpu_usage}%, Mem:{memory_info.percent}%). Decreasing concurrency to {new_limit}.")
            elif cpu_usage < 80 and memory_info.percent < 70:
                if current_limit < max_concurrency:
                    new_limit = min(max_concurrency, current_limit + 3)
                    app_logger.info(f"Low load (CPU:{cpu_usage}%, Mem:{memory_info.percent}%). Increasing concurrency to {new_limit}.")

            if new_limit != current_limit:
                await semaphore.adjust(new_limit)

            await asyncio.sleep(10)
    except asyncio.CancelledError:
        app_logger.info("Concurrency adjustment task canceled.")
    except Exception as e:
        app_logger.error(f"Error in adjust_concurrency: {e}", exc_info=True)

#######################################################################
#                       MAIN TASK: PROCESSING URLs
#######################################################################

async def process_url_wrapper(url: str, semaphore: AsyncSemaphore, current_run_data: List[Dict[str, str]]):
    global failed_submissions, retry_count
    await semaphore.acquire()
    browser = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=not DEBUG_MODE, slow_mo=50 if DEBUG_MODE else 0)
            context = await browser.new_context(storage_state=STORAGE_STATE)
            page = await context.new_page()
            extracted_data = await process_url(page, url)
            if extracted_data:
                async with asyncio.Lock():
                    current_run_data.append(extracted_data)
                with progress_lock:
                    progress["current"] += 1
                # If retries succeeded
                retries_for_url = url_retry_counts.get(url, 0)
                if retries_for_url > 0:
                    msg = f"URL '{url}' succeeded after {retries_for_url} retry attempt(s)."
                    successful_retries.append(msg)
                    app_logger.info(msg)
            else:
                # If no data returned
                raise Exception(f"Failed to extract data from {url}")
        await browser.close()
    except TimeoutError as te:
        failed_submissions += 1
        handle_processing_error(url, str(te))
    except Exception as e:
        retry_count += 1
        handle_processing_error(url, str(e))
    finally:
        if browser:
            await browser.close()
        semaphore.release()

def handle_processing_error(url: str, error_msg: str):
    """Handle errors during URL processing with retry logic."""
    global retry_urls, final_failed_urls
    current_retries = url_retry_counts.get(url, 0) + 1
    url_retry_counts[url] = current_retries
    if current_retries < MAX_TOTAL_RETRIES:
        retry_urls.append(url)
        app_logger.info(f"Retrying {url} (attempt {current_retries}/{MAX_TOTAL_RETRIES}) due to: {error_msg}")
    else:
        final_failed_urls.append(url)
        run_failures.append(f"URL {url} failed after max retries. Reason: {error_msg}")
        app_logger.error(f"Max retries reached for {url}. Reason: {error_msg}")

async def process_urls():
    global urls_data, stop_flag, pause_flag, progress, start_time, run_failures, successful_retries
    app_logger.info("Scheduled job 'process_urls' started.")
    stop_flag = False
    run_failures = []
    successful_retries.clear()
    load_default_data()

    if not urls_data:
        app_logger.error('No URLs to process')
        send_chat_message('No URLs to process')
        return

    ensure_storage_state()
    login_needed = await is_login_needed(urls_data[0])
    if login_needed:
        context = await login_and_get_context(headless=not DEBUG_MODE)
        if not context:
            app_logger.error('Login failed. Exiting process.')
            send_chat_message('Login failed. Exiting process.')
            return

    async_semaphore = AsyncSemaphore(config['initial_concurrency'])

    with progress_lock:
        progress["total"] = len(urls_data)
        progress["current"] = 0

    app_logger.info(f"Total URLs to process: {progress['total']}")
    update_last_run_time()

    current_run_data: List[Dict[str, str]] = []
    adjust_concurrency_task = asyncio.create_task(adjust_concurrency(async_semaphore))

    tasks: List[asyncio.Task] = []
    start_time = datetime.now()

    # Main processing
    for url in urls_data:
        if stop_flag or shutdown_event.is_set():
            break
        while pause_flag:
            await asyncio.sleep(1)
        tasks.append(asyncio.create_task(process_url_wrapper(url, async_semaphore, current_run_data)))
    await asyncio.gather(*tasks)

    # Retry logic
    retry_round = 1
    while retry_urls and retry_round <= MAX_TOTAL_RETRIES:
        if shutdown_event.is_set():
            break
        app_logger.info(f"Retry round {retry_round} for {len(retry_urls)} URLs.")
        to_retry = retry_urls.copy()
        retry_urls.clear()
        tasks = []
        for url in to_retry:
            if shutdown_event.is_set():
                break
            tasks.append(asyncio.create_task(process_url_wrapper(url, async_semaphore, current_run_data)))
        await asyncio.gather(*tasks)
        retry_round += 1

    # Stop adjusting concurrency
    adjust_concurrency_task.cancel()
    try:
        await adjust_concurrency_task
    except asyncio.CancelledError:
        pass

    end_time = datetime.now()
    total_time = (end_time - start_time).total_seconds()
    processed_stores = progress["current"]
    avg_url_processing_time = sum(url_processing_times) / len(url_processing_times) if url_processing_times else 0

    app_logger.info("URL Processing Summary:")
    app_logger.info(f"Total Time: {total_time:.2f}s")
    app_logger.info(f"Stores Processed: {processed_stores}")
    app_logger.info(f"Retries: {retry_count}")
    app_logger.info(f"Failed Submissions: {failed_submissions}")
    app_logger.info(f"Avg URL Time: {avg_url_processing_time:.2f}s")

    # Generate PDF
    await generate_pdf_report(
        total_time=total_time,
        processed_stores=processed_stores,
        retry_count=retry_count,
        failed_submissions=failed_submissions,
        avg_url_processing_time=avg_url_processing_time,
        avg_form_filling_time=(sum(form_filling_times)/len(form_filling_times) if form_filling_times else 0),
        failed_urls=final_failed_urls
    )

    # Submit forms
    await submit_all_forms(current_run_data)
    update_next_run_time()

    avg_form_filling_time = sum(form_filling_times)/len(form_filling_times) if form_filling_times else 0

    # Daily summary
    send_daily_summary(total_time, processed_stores, retry_count, failed_submissions, avg_url_processing_time, avg_form_filling_time)

    # Notify failed URLs if any
    if final_failed_urls:
        send_failed_urls(final_failed_urls)

    # Post-run summary
    if run_failures or successful_retries:
        summary_message = "### **Run Completed with Some Issues**\n"
        if successful_retries:
            summary_message += "#### URLs succeeded after retries:\n"
            for msg in successful_retries:
                summary_message += f"- {msg}\n"
        if run_failures:
            summary_message += "#### Final failed URLs and errors:\n"
            for issue in run_failures:
                summary_message += f"- {issue}\n"
        send_chat_message(summary_message)
    else:
        send_chat_message("✅ **Run Completed Successfully** with no issues.")

    app_logger.info("Scheduled job 'process_urls' completed.")


#######################################################################
#                           SCHEDULER & SCHEDULES
#######################################################################

def save_schedules():
    if scheduler:
        jobs = scheduler.get_jobs()
        schedules = []
        for job in jobs:
            next_run_time_dt = job.next_run_time
            next_run_time_str = next_run_time_dt.strftime('%Y-%m-%d %H:%M:%S') if next_run_time_dt else None
            schedules.append({
                'id': job.id,
                'name': job.name,
                'datetime': next_run_time_str,
                'repeat_daily': isinstance(job.trigger, IntervalTrigger)
            })
        with open(SCHEDULE_FILE, 'w') as f:
            json.dump(schedules, f)

def load_schedules():
    if os.path.exists(SCHEDULE_FILE):
        with open(SCHEDULE_FILE, 'r') as f:
            schedules = json.load(f)
            for schedule in schedules:
                job_id = schedule['id']
                name = schedule.get('name', 'Unnamed Schedule')
                datetime_str = schedule['datetime']
                repeat_daily = schedule['repeat_daily']
                if datetime_str:
                    next_run_time = datetime.strptime(datetime_str, '%Y-%m-%d %H:%M:%S')
                    next_run_time = LOCAL_TIMEZONE.localize(next_run_time)
                    if repeat_daily:
                        scheduler.add_job(
                            process_urls,
                            IntervalTrigger(days=1, start_date=next_run_time),
                            id=job_id,
                            name=name
                        )
                    else:
                        scheduler.add_job(
                            process_urls,
                            DateTrigger(run_date=next_run_time),
                            id=job_id,
                            name=name
                        )

#######################################################################
#                                 ROUTES
#######################################################################

@app.route('/')
async def index():
    return await render_template('index.html')

@app.route('/logs')
async def logs_page():
    return await render_template('logs.html')

@app.route('/start_now', methods=['POST'])
async def start_now():
    asyncio.create_task(process_urls())
    open('app.log', 'w').close()
    return jsonify(status="Extraction started")

@app.route('/stop', methods=['POST'])
async def stop_extraction():
    global stop_flag, progress
    stop_flag = True
    with progress_lock:
        progress = {"current": 0, "total": 0, "currentUrl": "N/A", "lastStore": "N/A", "lastUpdate": "N/A"}
    app_logger.info('Extraction stopped and progress reset.')

    # Terminate Playwright Chromium processes
    for proc in psutil.process_iter():
        try:
            if proc.name().lower() in ["chrome.exe", "chromium"]:
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    return jsonify(status="Extraction stopped")

@app.route('/clear_log', methods=['POST'])
async def clear_log():
    try:
        open('app.log', 'w').close()
        return jsonify(status="Log cleared")
    except Exception:
        app_logger.error("Failed to clear the log file.", exc_info=True)
        return jsonify(status="Error clearing log")

@app.route('/progress_status')
async def progress_status():
    with progress_lock:
        total = progress.get("total", 0)
        current = progress.get("current", 0)
        percentage = (current / total) * 100 if total > 0 else 0

        try:
            with open('app.log', 'r') as log_file:
                logs = log_file.readlines()
            if logs:
                latest_log = logs[-1].strip()
                # Extract the time and message from log
                log_time = latest_log.split(",")[0].split(" ")[1][:5]
                formatted_log = f"{log_time}: {latest_log.split(' ', 2)[2]}"
            else:
                formatted_log = "N/A"
        except Exception:
            formatted_log = "N/A"

    return jsonify(progress=progress, percentage=percentage, latestLog=formatted_log)

@app.route('/log')
async def log_status():
    try:
        with open('app.log', 'r') as log_file:
            logs = log_file.readlines()
        formatted_logs = []
        for log in logs:
            if 'submitted' in log:
                formatted_logs.append(log.strip())
            elif 'Failed to process URL' in log:
                formatted_logs.append('<span class="log-entry error">Failed to process URL.</span>')
            else:
                formatted_logs.append(log.strip())
        return jsonify(logs=formatted_logs)
    except Exception:
        app_logger.error("Failed to read the log file.", exc_info=True)
        return jsonify(logs=[])

@app.route('/stats', methods=['GET'])
async def stats():
    global last_run_time, next_run_time, progress
    update_next_run_time()
    return jsonify({
        "last_run_time": last_run_time,
        "next_run_time": next_run_time,
        "total_urls": progress["total"],
        "processed_urls": progress["current"]
    })

@app.route('/toggle_schedule/<job_id>', methods=['POST'])
async def toggle_schedule(job_id):
    try:
        job = scheduler.get_job(job_id)
        if job:
            if job.next_run_time:
                scheduler.pause_job(job_id)
                status = 'paused'
            else:
                scheduler.resume_job(job_id)
                status = 'active'
            save_schedules()
            return jsonify(success=True, status=status)
        else:
            return jsonify(success=False, error="Job not found")
    except Exception as e:
        app_logger.error("Failed to toggle schedule.", exc_info=True)
        return jsonify(success=False, error=str(e))

@app.route('/delete_schedule/<job_id>', methods=['POST'])
async def delete_schedule(job_id):
    try:
        scheduler.remove_job(job_id)
        save_schedules()
        return jsonify(success=True)
    except Exception as e:
        app_logger.error("Failed to delete schedule.", exc_info=True)
        return jsonify(success=False, error=str(e))

@app.route('/toggle_repeat_daily/<job_id>', methods=['POST'])
async def toggle_repeat_daily(job_id):
    try:
        job = scheduler.get_job(job_id)
        if job:
            data = await request.get_json()
            repeat_daily = data.get('repeat_daily', False)
            next_run_time = job.next_run_time
            scheduler.remove_job(job_id)
            if repeat_daily and next_run_time:
                scheduler.add_job(process_urls, IntervalTrigger(days=1, start_date=next_run_time), id=job_id, name=job.name)
            elif next_run_time:
                scheduler.add_job(process_urls, DateTrigger(run_date=next_run_time), id=job_id, name=job.name)
            save_schedules()
            return jsonify(success=True)
        else:
            return jsonify(success=False, error="Job not found")
    except Exception as e:
        app_logger.error("Failed to toggle repeat daily status.", exc_info=True)
        return jsonify(success=False, error=str(e))

@app.route('/add_schedule', methods=['POST'])
async def add_schedule():
    try:
        data = await request.get_json()
        name = data.get('name', 'Unnamed Schedule')
        datetime_str = data['datetime']
        repeat_daily = data['repeat_daily']

        new_datetime = datetime.strptime(datetime_str, '%Y-%m-%d %H:%M:%S')
        new_datetime = LOCAL_TIMEZONE.localize(new_datetime)

        if new_datetime < datetime.now(LOCAL_TIMEZONE):
            return jsonify({'success': False, 'message': 'Start time must be in the future'}), 400

        job_id = f"job_{len(scheduler.get_jobs()) + 1}"

        if repeat_daily:
            scheduler.add_job(process_urls, IntervalTrigger(days=1, start_date=new_datetime), id=job_id, name=name)
        else:
            scheduler.add_job(process_urls, DateTrigger(run_date=new_datetime), id=job_id, name=name)

        save_schedules()
        return jsonify({'success': True, 'message': 'Schedule added successfully'})
    except Exception as e:
        app_logger.error("Failed to add schedule.", exc_info=True)
        return jsonify({'success': False, 'message': 'Internal server error'}), 500

@app.route('/edit_schedule/<job_id>', methods=['POST'])
async def edit_schedule(job_id):
    try:
        data = await request.get_json()
        name = data.get('name', 'Unnamed Schedule')
        datetime_str = data['datetime']
        repeat_daily = data['repeat_daily']

        new_datetime = datetime.strptime(datetime_str, '%Y-%m-%d %H:%M:%S')
        new_datetime = LOCAL_TIMEZONE.localize(new_datetime)

        if new_datetime < datetime.now(LOCAL_TIMEZONE):
            return jsonify({'success': False, 'message': 'Start time must be in the future'}), 400

        job = scheduler.get_job(job_id)
        if not job:
            return jsonify({'success': False, 'message': 'Job not found'}), 404

        scheduler.remove_job(job_id)
        if repeat_daily:
            scheduler.add_job(process_urls, IntervalTrigger(days=1, start_date=new_datetime), id=job_id, name=name)
        else:
            scheduler.add_job(process_urls, DateTrigger(run_date=new_datetime), id=job_id, name=name)

        save_schedules()
        return jsonify({'success': True, 'message': 'Schedule updated successfully'})
    except Exception as e:
        app_logger.error("Failed to update schedule.", exc_info=True)
        return jsonify({'success': False, 'message': 'Internal server error'}), 500

@app.route('/schedules', methods=['GET'])
async def get_schedules():
    jobs = scheduler.get_jobs()
    schedules = []
    for job in jobs:
        next_run_time_dt = job.next_run_time
        next_run_time_str = next_run_time_dt.strftime('%Y-%m-%d %H:%M:%S') if next_run_time_dt else None
        schedules.append({
            'id': job.id,
            'name': job.name,
            'datetime': next_run_time_str,
            'repeat_daily': isinstance(job.trigger, IntervalTrigger)
        })
    return jsonify({'schedules': schedules})

@app.route('/test_schedule', methods=['POST'])
async def test_schedule():
    try:
        data = await request.get_json()
        run_time_str = data.get('run_time')
        run_time = datetime.strptime(run_time_str, '%Y-%m-%d %H:%M:%S')
        run_time = LOCAL_TIMEZONE.localize(run_time)

        if run_time < datetime.now(LOCAL_TIMEZONE):
            return jsonify({'success': False, 'message': 'Run time must be in the future'}), 400

        scheduler.add_job(
            lambda: app_logger.info("Test job executed."),
            DateTrigger(run_date=run_time),
            id="test_job",
            name="Test Job"
        )

        save_schedules()
        return jsonify({'success': True, 'message': 'Test job scheduled successfully'})
    except Exception as e:
        app_logger.error("Failed to schedule test job.", exc_info=True)
        return jsonify({'success': False, 'message': 'Internal server error'}), 500

@app.route('/api/logs/download', methods=['GET'])
async def download_logs():
    logs = read_submission_logs()

    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    store = request.args.get('store')
    infFilter = request.args.get('infFilter')

    if start_date:
        logs = [log for log in logs if log['timestamp'] >= start_date]
    if end_date:
        logs = [log for log in logs if log['timestamp'] <= end_date]
    if store:
        logs = [log for log in logs if log['store'] == store]
    if infFilter:
        logs = [log for log in logs if float(log['inf'].strip('%')) > float(infFilter)]

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=['timestamp', 'store', 'orders', 'units', 'fulfilled', 'uph', 'inf', 'found', 'cancelled', 'lates', 'time_available'])
    writer.writeheader()
    writer.writerows(logs)
    output.seek(0)

    memory_file = io.BytesIO()
    memory_file.write(output.getvalue().encode('utf-8'))
    memory_file.seek(0)

    headers = {
        'Content-Disposition': 'attachment; filename=logs.csv'
    }

    return Response(memory_file.read(), mimetype='text/csv', headers=headers)

@app.route('/api/logs/summary', methods=['GET'])
async def get_logs_summary():
    logs = read_submission_logs()
    total_logs = len(logs)
    average_uph = sum(float(log['uph']) for log in logs) / total_logs if total_logs else 0
    latest_log_date = max(log['timestamp'] for log in logs) if logs else 'N/A'
    total_orders = sum(int(log['orders']) for log in logs)
    average_inf = sum(float(log['inf'].strip('%')) for log in logs) / total_logs if total_logs else 0

    summary = {
        'total_logs': total_logs,
        'average_uph': average_uph,
        'latest_log_date': latest_log_date,
        'total_orders': total_orders,
        'average_inf': average_inf
    }

    return jsonify(summary)

@app.route('/api/logs/latest_per_store', methods=['GET'])
async def get_latest_per_store():
    logs = read_submission_logs()
    latest_logs = {}

    for log in logs:
        log_date = log['timestamp'].split(' ')[0]
        store = log['store']
        key = (log_date, store)

        if key not in latest_logs or log['timestamp'] > latest_logs[key]['timestamp']:
            latest_logs[key] = log

    return jsonify(list(latest_logs.values()))

@app.route('/api/logs', methods=['GET'])
async def get_logs():
    logs = read_submission_logs()

    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    store = request.args.get('store')
    infFilter = request.args.get('infFilter')

    if start_date:
        logs = [log for log in logs if log['timestamp'] >= start_date]
    if end_date:
        logs = [log for log in logs if log['timestamp'] <= end_date]
    if store:
        logs = [log for log in logs if log['store'] == store]
    if infFilter:
        logs = [log for log in logs if float(log['inf'].strip('%')) > float(infFilter)]

    return jsonify(logs)

#######################################################################
#                              LIFECYCLE HOOKS
#######################################################################

@app.before_serving
async def startup():
    global scheduler
    app_logger.info("Starting scheduler and loading schedules.")
    scheduler = AsyncIOScheduler(event_loop=asyncio.get_event_loop())
    load_schedules()
    scheduler.start()

@app.after_serving
async def shutdown_handler():
    await shutdown()

#######################################################################
#                                MAIN ENTRY
#######################################################################

if __name__ == "__main__":
    if not os.path.exists('output'):
        os.makedirs('output')
    try:
        app.run(host='0.0.0.0', port=5002)
    finally:
        asyncio.run(shutdown())
