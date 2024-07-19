import logging
from datetime import datetime
from quart import Quart, jsonify, render_template, request, send_file, Response
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from playwright.async_api import async_playwright, Page, BrowserContext
import os
import re
import requests
import traceback
import csv
import json
import io
import psutil
import asyncio
from threading import Thread, Lock
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Dict

# Load configuration from config.json
with open('config.json', 'r') as config_file:
    config = json.load(config_file)

app = Quart(__name__)
app.secret_key = config['secret_key']

scheduler = BackgroundScheduler()
scheduler.start()

SCHEDULE_FILE = 'schedules.json'
LOG_FILE = os.path.join('output', 'submissions.log')

log_lock = Lock()
progress_lock = Lock()

# Setup logging
def setup_logging():
    app_logger = logging.getLogger('app')
    app_logger.setLevel(logging.INFO)

    app_file_handler = logging.FileHandler('app.log')
    console_handler = logging.StreamHandler()

    app_file_handler.setLevel(logging.INFO)
    console_handler.setLevel(logging.INFO)

    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    app_file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    app_logger.addHandler(app_file_handler)
    app_logger.addHandler(console_handler)

    return app_logger

app_logger = setup_logging()

werkzeug_logger = logging.getLogger('werkzeug')
werkzeug_logger.setLevel(logging.ERROR)

FORM_URL = config['form_url']
LOGIN_URL = config['login_url']

urls_data = []
stop_flag = False
pause_flag = False
login_required = False
otp_required = False
is_logged_in = False
progress = {"current": 0, "total": 0, "currentUrl": "N/A", "lastStore": "N/A", "lastUpdate": "N/A"}
otp = None
last_run_time = None
next_run_time = None

otp_pattern = r'(\d{4,8})'

def load_default_data():
    global urls_data
    try:
        with open('urls.csv', 'r') as file:
            urls_data = [row.strip() for row in file.readlines()]
        app_logger.info(f'{len(urls_data)} URLs loaded from urls.csv')
    except Exception as e:
        app_logger.error("Failed to load the default CSV file. Please check the file path and format.")

def update_last_run_time():
    global last_run_time
    last_run_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def update_next_run_time():
    global next_run_time
    jobs = scheduler.get_jobs()
    if jobs:
        next_run_time = jobs[0].next_run_time.strftime('%Y-%m-%d %H:%M:%S')
    else:
        next_run_time = None

def ensure_storage_state():
    state_file = 'state.json'
    if not os.path.exists(state_file) or os.path.getsize(state_file) == 0:
        with open(state_file, 'w') as f:
            json.dump({}, f)

async def login_and_get_context() -> Optional[BrowserContext]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        if await perform_login_and_otp(page):
            await context.storage_state(path='state.json')
            await browser.close()
            return context
        await browser.close()
        return None

async def perform_login_and_otp(page: Page) -> bool:
    global login_required, otp_required, is_logged_in
    try:
        await page.goto(LOGIN_URL)
        await page.fill('input[id="ap_email"]', config['login_email'])
        await page.fill('input[id="ap_password"]', config['login_password'])
        await page.click('input[id="signInSubmit"]')
        
        otp_field = await page.wait_for_selector('input[type="tel"]', timeout=30000)
        if otp_field:
            app_logger.info("Waiting for OTP to be received...")
            await asyncio.sleep(20)
            os.environ["NO_PROXY"] = "13.49.230.218"
            otp_url = "http://13.49.230.218:5003/get_otp"
            while True:
                response = requests.get(otp_url)
                otp_code = response.text.strip()
                if otp_code and otp_code != 'No OTP yet':
                    break
                app_logger.info("Waiting for OTP...")
                await asyncio.sleep(10)

            await page.fill('input[type="tel"]', otp_code)
            await page.get_by_label("Don't require OTP on this").check()
            await page.get_by_label("Sign in").click()
            await asyncio.sleep(10)
            login_required = False
            otp_required = False
            is_logged_in = True

            await page.context.storage_state(path='state.json')
            return True
        else:
            otp_required = True
    except Exception as e:
        app_logger.error("Login and OTP verification failed. Please check your credentials and OTP settings.")
    return False

async def process_url(page: Page, url: str, retries=3) -> Optional[Dict[str, str]]:
    global login_required, otp_required, is_logged_in

    for attempt in range(retries):
        if stop_flag:
            return None
        try:
            await page.goto(url)

            if "signin" in page.url or "ap/signin" in page.url:
                app_logger.info("Detected sign-in requirement.")
                if not is_logged_in and await perform_login_and_otp(page):
                    continue
                else:
                    return None

            store_element = await page.wait_for_selector('#partner-switcher button span b', timeout=8000)
            if not store_element:
                continue
            store = await store_element.inner_text()

            last_cell_xpath = '//*[@id="content"]/div/div[2]/div[2]/kat-tabs/kat-tab[1]/div/div[2]/kat-table/kat-table-head/kat-table-row[2]/kat-table-cell[11]/div'
            await page.wait_for_selector(last_cell_xpath, timeout=10000)

            row_xpath = '//*[@id="content"]/div/div[2]/div[2]/kat-tabs/kat-tab[1]/div/div[2]/kat-table/kat-table-head/kat-table-row[2]'
            row_element = await page.wait_for_selector(row_xpath, timeout=8000)
            if not row_element:
                continue

            cells = await row_element.query_selector_all('kat-table-cell')
            row_data = [await cell.inner_text() for cell in cells]

            if len(row_data) >= 11:
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

                availability_tab_xpath = '//span[@slot="label" and text()="Availability"]'
                try:
                    await page.click(availability_tab_xpath)

                    availability_table_xpath = '//*[@id="content"]/div/div[2]/div[2]/kat-tabs/kat-tab[4]/div/div[2]/kat-table/kat-table-head/kat-table-row[2]/kat-table-cell[7]/div'
                    availability_data = await page.wait_for_selector(availability_table_xpath, timeout=5000)
                    if availability_data:
                        data['availability'] = await availability_data.inner_text()

                    non_blank_data_points = sum(1 for key, value in data.items() if value.strip())
                    if non_blank_data_points > 4:
                        return data
                    else:
                        continue
                except Exception as e:
                    continue
            break
        except Exception as e:
            app_logger.error("Failed to process URL. Please check the website availability and layout.")
            if attempt < retries - 1:
                await asyncio.sleep(1)
            else:
                app_logger.error(f"Max retries reached for {url}. Skipping.")
    return None

async def fill_google_form(page: Page, data: Dict[str, str], current: int, total: int):
    await page.goto(FORM_URL)

    try:
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
                app_logger.error(f"Error filling form field {label} with data {data[key]}: {e}")

        try:
            await page.get_by_label("Submit", exact=True).click()

            await page.wait_for_selector("//div[contains(text(),'Your response has been recorded.')]", timeout=20000)
        except Exception as e:
            app_logger.error(f"Error during form submission: {e}")

        log_entry = f"{datetime.now().strftime('%H:%M')} {data['store']} submitted (Orders {data['orders']}, Units {data['units']}, Fulfilled {data['fulfilled']}, UPH {data['uph']}, INF {data['inf']}, Found {data['found']}, Cancelled {data['cancelled']}, Lates {data['lates']}, Time available: {data['availability']})"
        
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

    except Exception as e:
        app_logger.error("Failed to fill out the Google Form. Please check the form layout and fields.")

def log_submission(data):
    log_file = LOG_FILE
    fieldnames = ['timestamp', 'store', 'orders', 'units', 'fulfilled', 'uph', 'inf', 'found', 'cancelled', 'lates', 'time_available']

    try:
        logs = []
        with log_lock:
            if os.path.exists(log_file):
                with open(log_file, 'r') as csvfile:
                    reader = csv.DictReader(csvfile)
                    logs = list(reader)

            new_log_date = data['timestamp'].split(' ')[0]
            new_store = data['store']
            logs = [log for log in logs if not (log['timestamp'].split(' ')[0] == new_log_date and log['store'] == new_store)]

            logs.append(data)

            with open(log_file, 'w', newline='') as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(logs)
    except IOError:
        app_logger.error("I/O error occurred while logging submission data.")

async def process_url_wrapper(url: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state='state.json')
        page = await context.new_page()
        extracted_data = await process_url(page, url)
        if extracted_data:
            await fill_google_form(page, extracted_data, progress['current'], progress['total'])
        await browser.close()

async def process_urls():
    global urls_data, stop_flag, pause_flag, progress
    stop_flag = False
    load_default_data()

    if not urls_data:
        app_logger.error('No URLs to process')
        return

    ensure_storage_state()

    context = await login_and_get_context()
    if not context:
        app_logger.error('Login failed. Exiting process.')
        return

    with progress_lock:
        progress["total"] = len(urls_data)
        progress["current"] = 0
    app_logger.info(f"Total URLs to process: {progress['total']}")
    update_last_run_time()

    first_url = urls_data[0]
    await process_url_wrapper(first_url)  # Process the first URL to validate

    tasks = []
    failed_urls = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        loop = asyncio.get_event_loop()
        futures = {executor.submit(asyncio.run, process_url_wrapper(url)): url for url in urls_data[1:]}  # Start from the second URL

        for future in as_completed(futures):
            url = futures[future]
            try:
                data = future.result()
                if data:
                    with progress_lock:
                        progress["current"] += 1
            except Exception as e:
                app_logger.error(f"Failed to process URL: {url}")
                failed_urls.append(url)

    with progress_lock:
        progress["currentUrl"] = "N/A"
        progress["lastStore"] = "N/A"
    
    if failed_urls:
        app_logger.info(f"Retrying {len(failed_urls)} failed URLs...")
        stop_flag = False
        for url in failed_urls:
            if stop_flag:
                break
            while pause_flag:
                await asyncio.sleep(1)
            await process_url_wrapper(url)
    
    update_next_run_time()

@app.route('/')
async def index():
    return await render_template('index.html')

@app.route('/logs')
async def logs():
    return await render_template('logs.html')

@app.route('/start_now', methods=['POST'])
async def start_now():
    thread = Thread(target=asyncio.run, args=(process_urls(),))
    thread.start()
    open('app.log', 'w').close()
    return jsonify(status="Extraction started")

@app.route('/stop', methods=['POST'])
async def stop_extraction():
    global stop_flag, progress
    stop_flag = True
    with progress_lock:
        progress = {"current": 0, "total": 0, "currentUrl": "N/A", "lastStore": "N/A", "lastUpdate": "N/A"}
    app_logger.info('Extraction stopped and progress reset.')

    for proc in psutil.process_iter():
        if proc.name().lower() == "chrome.exe":
            proc.kill()
    
    return jsonify(status="Extraction stopped")

@app.route('/clear_log', methods=['POST'])
async def clear_log():
    try:
        open('app.log', 'w').close()
        return jsonify(status="Log cleared")
    except Exception as e:
        app_logger.error("Failed to clear the log file.")
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
                log_time = latest_log.split(",")[0].split(" ")[1][:5]
                formatted_log = f"{log_time}: {latest_log.split(' ', 2)[2]}"
            else:
                formatted_log = "N/A"
        except Exception as e:
            formatted_log = "N/A"
            app_logger.error("Failed to read the log file for progress status.")

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
                formatted_logs.append('<span class="log-entry error">Failed to process URL. Please check the website availability and layout.</span>')
            else:
                formatted_logs.append(log.strip())
        return jsonify(logs=formatted_logs)
    except Exception as e:
        app_logger.error("Failed to read the log file.")
        return jsonify(logs=[])

@app.route('/stats')
async def stats():
    global last_run_time, next_run_time, progress
    update_next_run_time()
    stats = {
        "last_run_time": last_run_time,
        "next_run_time": next_run_time,
        "total_urls": progress["total"],
        "processed_urls": progress["current"]
    }
    return jsonify(stats)

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
        app_logger.error("Failed to toggle schedule.")
        return jsonify(success=False, error=str(e))

@app.route('/delete_schedule/<job_id>', methods=['POST'])
async def delete_schedule(job_id):
    try:
        scheduler.remove_job(job_id)
        save_schedules()
        return jsonify(success=True)
    except Exception as e:
        app_logger.error("Failed to delete schedule.")
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
            if repeat_daily:
                scheduler.add_job(process_urls, IntervalTrigger(days=1, start_date=next_run_time), id=job_id)
            else:
                scheduler.add_job(process_urls, DateTrigger(run_date=next_run_time), id=job_id)
            save_schedules()
            return jsonify(success=True)
        else:
            return jsonify(success=False, error="Job not found")
    except Exception as e:
        app_logger.error("Failed to toggle repeat daily status.")
        return jsonify(success=False, error=str(e))

@app.route('/get_otp', methods=['GET'])
async def get_otp():
    global otp
    return otp if otp else 'No OTP yet', 200

@app.route('/sms', methods=['POST'])
async def receive_sms():
    global otp
    sms_content = await request.form
    sms = sms_content.get('sms', '')

    match = re.search(otp_pattern, sms)
    if match:
        otp = match.group(1)
        app_logger.info(f"Extracted OTP: {otp}")

    return 'SMS received', 200

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

def read_submission_logs():
    logs = []
    try:
        with log_lock:
            if os.path.exists(LOG_FILE):
                with open(LOG_FILE, 'r') as csvfile:
                    reader = csv.DictReader(csvfile)
                    for row in reader:
                        logs.append(row)
    except FileNotFoundError:
        app_logger.error("Submission log file not found.")
    return logs

def save_schedules():
    jobs = scheduler.get_jobs()
    schedules = []
    for job in jobs:
        schedules.append({
            'id': job.id,
            'name': job.name,
            'datetime': job.next_run_time.strftime('%Y-%m-%d %H:%M:%S'),
            'repeat_daily': isinstance(job.trigger, IntervalTrigger)
        })
    with open(SCHEDULE_FILE, 'w') as f:
        json.dump(schedules, f)

def load_schedules():
    if os.path.exists(SCHEDULE_FILE):
        with open(SCHEDULE_FILE, 'r') as (f):
            schedules = json.load(f)
            for schedule in schedules:
                job_id = schedule['id']
                name = schedule.get('name', 'Unnamed Schedule')
                next_run_time = datetime.strptime(schedule['datetime'], '%Y-%m-%d %H:%M:%S')
                repeat_daily = schedule['repeat_daily']
                if repeat_daily:
                    scheduler.add_job(process_urls, IntervalTrigger(days=1, start_date=next_run_time), id=job_id, name=name)
                else:
                    scheduler.add_job(process_urls, DateTrigger(run_date=next_run_time), id=job_id, name=name)

@app.route('/edit_schedule/<id>', methods=['POST'])
async def edit_schedule(id):
    try:
        data = await request.get_json()
        name = data.get('name', 'Unnamed Schedule')
        new_datetime = datetime.strptime(data['datetime'], '%Y-%m-%d %H:%M:%S')
        repeat_daily = data['repeat_daily']

        job = scheduler.get_job(id)
        if not job:
            return jsonify({'success': False, 'message': 'Job not found'}), 404

        scheduler.remove_job(id)

        if repeat_daily:
            scheduler.add_job(process_urls, IntervalTrigger(days=1, start_date=new_datetime), id=id, name=name)
        else:
            scheduler.add_job(process_urls, DateTrigger(run_date=new_datetime), id=id, name=name)

        save_schedules()
        return jsonify({'success': True, 'message': 'Schedule updated successfully'})
    except Exception as e:
        app.logger.error("Failed to update schedule.")
        return jsonify({'success': False, 'message': 'Internal server error'}), 500

@app.route('/add_schedule', methods=['POST'])
async def add_schedule():
    try:
        data = await request.get_json()
        name = data.get('name', 'Unnamed Schedule')
        datetime_str = data['datetime']
        repeat_daily = data['repeat_daily']
        job_id = f"job_{len(scheduler.get_jobs()) + 1}"

        new_datetime = datetime.strptime(datetime_str, '%Y-%m-%d %H:%M:%S')
        if repeat_daily:
            scheduler.add_job(process_urls, IntervalTrigger(days=1, start_date=new_datetime), id=job_id, name=name)
        else:
            scheduler.add_job(process_urls, DateTrigger(run_date=new_datetime), id=job_id, name=name)

        save_schedules()
        return jsonify({'success': True, 'message': 'Schedule added successfully'})
    except Exception as e:
        app.logger.error("Failed to add schedule.")
        return jsonify({'success': False, 'message': 'Internal server error'}), 500

@app.route('/schedules', methods=['GET'])
async def get_schedules():
    jobs = scheduler.get_jobs()
    schedules = []
    for job in jobs:
        schedules.append({
            'id': job.id,
            'name': job.name,
            'datetime': job.next_run_time.strftime('%Y-%m-%d %H:%M:%S'),
            'repeat_daily': isinstance(job.trigger, IntervalTrigger)
        })
    return jsonify({'schedules': schedules})

load_schedules()

if __name__ == "__main__":
    if not os.path.exists('output'):
        os.makedirs('output')
    app.run(host='0.0.0.0', port=5002)
