import asyncio
import json
import os
import re
import requests
import logging
from datetime import datetime
from threading import Thread, Event
from playwright.async_api import async_playwright, Page, BrowserContext, TimeoutError
from typing import Optional, List
from tkinter import Tk, Label, Entry, Button, filedialog, StringVar, IntVar, messagebox, Text, Scrollbar, END, VERTICAL, Frame
from tkinter.ttk import Progressbar
from idlelib.tooltip import Hovertip

# Setup logging
class TextHandler(logging.Handler):
    """This class allows logging to a Tkinter Text widget"""
    def __init__(self, text_widget):
        logging.Handler.__init__(self)
        self.text_widget = text_widget

    def emit(self, record):
        msg = self.format(record)
        def append():
            self.text_widget.configure(state='normal')
            self.text_widget.insert(END, msg + '\n')
            self.text_widget.configure(state='disabled')
            self.text_widget.yview(END)
        self.text_widget.after(0, append)

# Create a global logger instance
logger = logging.getLogger('mini_scraper')
logger.setLevel(logging.INFO)

# Default global variables
config = {
    "login_url": "https://sellercentral.amazon.co.uk/ap/signin?openid.pape.max_auth_age=900&openid.return_to=https%3A%2F%2Fsellercentral.amazon.co.uk%2Fsnowdash%3Fmons_sel_dir_mcid%3Damzn1.merchant.d.ADCTYK7ZDZ3AJG55TWLDKHVPEU7Q%26mons_sel_mkid%3DAM7DNVYQULIQ5%26mons_sel_dir_paid%3Damzn1.pa.d.AC5SCORDKVABET36OI32HPSR7K3A%26ignore_selection_changed%3Dtrue&openid.identity=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select&openid.assoc_handle=sc_uk_amazon_v2&openid.mode=checkid_setup&openid.claimed_id=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0%2Fidentifier_select&openid.ns=http%3A%2F%2Fspecs.openid.net%2Fauth%2F2.0&mons_redirect=sign_in&ssoResponse=eyJ6aXAiOiJERUYiLCJlbmMiOiJBMjU2R0NNIiwiYWxnIjoiQTI1NktXIn0.nIxDt6YB6oQ20szF9uPcUAsvJeUex6GYuJC79NFGEc_0g5C0RKA0hw.9vWwKREZ-4z4ZT8N._i6p8USp4eccQS46LYe47Th1ZBSFiWsiJhb2srIh7niZisOyGeT6kL2gCSLSTHRZwElkCN6AMqcX5kqsZ3H3PqYtouGCc34W2fhFaMc3dZ9eEonHaM0zMQ3bRaJXqehr4at6YdLv3Si3JMkh8vouqCAwr9Wq_jxu0nDxekiJtkdpVqOCTI68BM_Ce9DFBd_XS5NFOTw8.MEIbisUMWZaNBcRUH9M5nw",
    "form_url": "https://docs.google.com/forms/d/e/1FAIpQLScg_jnxbuJsPs4KejUaVuu-HfMQKA3vSXZkWaYh-P_lbjE56A/viewform?hl=en",
    "login_email": "niki.cooke@morrisonsplc.co.uk",
    "login_password": "Nextfriday"
}
LOGIN_URL = config['login_url']
FORM_URL = config['form_url']
STORAGE_STATE = 'state.json'
urls_data = []
stop_event = Event()

# Function to load configuration
def load_config(file_path):
    global LOGIN_URL, FORM_URL, config
    try:
        with open(file_path, 'r') as config_file:
            config = json.load(config_file)
            assert 'login_url' in config and 'form_url' in config and 'login_email' in config and 'login_password' in config, \
                "Missing required config parameters"
            LOGIN_URL = config['login_url']
            FORM_URL = config['form_url']
            return config
    except Exception as e:
        logger.critical("Failed to load configuration.", exc_info=True)
        raise SystemExit(e)

# Function to save configuration
def save_config(file_path):
    global config
    try:
        with open(file_path, 'w') as config_file:
            json.dump(config, config_file, indent=4)
        logger.info("Configuration saved successfully.")
    except Exception as e:
        logger.error("Failed to save configuration.", exc_info=True)

# Function to load URLs from CSV
def load_urls(file_path):
    global urls_data
    try:
        with open(file_path, 'r') as file:
            urls_data = [row.strip() for row in file.readlines()]
        if not urls_data:
            raise ValueError("URLs data is empty")
        logger.info(f'{len(urls_data)} URLs loaded from {file_path}')
    except Exception as e:
        logger.critical("Failed to load the default CSV file. Exiting...", exc_info=True)
        raise SystemExit(e)

# Perform login and OTP verification
async def perform_login_and_otp(page: Page) -> bool:
    try:
        await page.goto(LOGIN_URL)
        await page.fill('input[id="ap_email"]', config['login_email'])
        await page.fill('input[id="ap_password"]', config['login_password'])
        await page.click('input[id="signInSubmit"]')

        otp_field = await page.wait_for_selector('input[type="tel"]', timeout=30000)
        if otp_field:
            await asyncio.sleep(20)
            otp_url = "http://13.49.230.218:5003/get_otp"
            while True:
                try:
                    response = requests.get(otp_url)
                    response.raise_for_status()
                    otp_code = response.text.strip()
                    if otp_code and otp_code != 'No OTP yet':
                        break
                except requests.RequestException as req_e:
                    logger.error("Failed to retrieve OTP", exc_info=True)
                await asyncio.sleep(10)

            await page.fill('input[type="tel"]', otp_code)
            await page.get_by_label("Don't require OTP on this").check()
            await page.get_by_label("Sign in").click()
            await asyncio.sleep(10)

            await page.context.storage_state(path=STORAGE_STATE)
            return True
    except Exception as e:
        logger.error("Login and OTP verification failed.", exc_info=True)
    return False

# Retry with exponential backoff
async def wait_for_selector_with_retry(page: Page, selector: str, retries: int = 3, base_delay: int = 1):
    delay = base_delay
    for attempt in range(retries):
        try:
            return await page.wait_for_selector(selector, timeout=10000)
        except TimeoutError:
            if attempt < retries - 1:
                logger.warning(f"Timeout waiting for selector {selector}, retrying in {delay} seconds...")
                await asyncio.sleep(delay)
                delay *= 2
            else:
                logger.error(f"Timeout waiting for selector {selector} after {retries} attempts.")
                raise

# Process URL and extract data
async def process_url(page: Page, url: str, retries=3) -> Optional[dict]:
    for attempt in range(retries):
        if stop_event.is_set():
            logger.info(f"Stopping processing for URL: {url}")
            return None
        logger.info(f"Processing URL: {url}, attempt {attempt + 1}")
        try:
            await page.goto(url)
            if "signin" in page.url or "ap/signin" in page.url:
                logger.info(f"Sign-in required for {url}. Skipping this URL.")
                return None

            store_element = await wait_for_selector_with_retry(page, '#partner-switcher button span b')
            if not store_element:
                logger.warning(f"Store element not found for {url}")
                continue

            store = await store_element.inner_text()
            if not store:
                logger.warning(f"Store name not found for {url}")
                continue

            last_cell_xpath = '//*[@id="content"]/div/div[2]/div[2]/kat-tabs/kat-tab[1]/div/div[2]/kat-table/kat-table-head/kat-table-row[2]/kat-table-cell[11]/div'
            await wait_for_selector_with_retry(page, last_cell_xpath)

            row_xpath = '//*[@id="content"]/div/div[2]/div[2]/kat-tabs/kat-tab[1]/div/div[2]/kat-table/kat-table-head/kat-table-row[2]'
            row_element = await wait_for_selector_with_retry(page, row_xpath)
            if not row_element:
                logger.warning(f"Row element not found for {url}")
                continue

            cells = await row_element.query_selector_all('kat-table-cell')
            if not cells:
                logger.warning(f"Cells not found for {url}")
                continue

            row_data = [await cell.inner_text() for cell in cells]
            if not row_data:
                logger.warning(f"Row data not found for {url}")
                continue

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

                # Validate fields 3, 4, and 5
                if not data['orders'] or not data['units'] or not data['fulfilled']:
                    logger.warning(f"Fields 3, 4, and 5 are missing for {url}. Retrying...")
                    continue

                availability_tab_xpath = '//span[@slot="label" and text()="Availability"]'
                try:
                    await page.click(availability_tab_xpath)
                    availability_table_xpath = '//*[@id="content"]/div/div[2]/div[2]/kat-tabs/kat-tab[4]/div/div[2]/kat-table/kat-table-head/kat-table-row[2]/kat-table-cell[7]/div'
                    availability_data = await wait_for_selector_with_retry(page, availability_table_xpath)
                    if availability_data:
                        availability_text = await availability_data.inner_text()
                        if availability_text:
                            data['availability'] = availability_text

                    logger.info(f"Successfully processed URL: {url}")
                    return data
                except Exception as e:
                    logger.error("Error extracting availability data.", exc_info=True)
                    continue
            else:
                logger.warning(f"Insufficient row data for {url}")
            break
        except Exception as e:
            logger.error("Failed to process URL. Please check the website availability and layout.", exc_info=True)
    logger.info(f"Failed to process URL after {retries} attempts: {url}")
    return None

# Fill Google form with extracted data
async def fill_google_form(page: Page, data: dict):
    try:
        await page.goto(FORM_URL)
    except Exception as e:
        logger.error("Failed to load Google Form URL.", exc_info=True)
        return

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
                logger.error(f"Error filling form field {label} with data {data[key]}: {e}")

        try:
            await page.get_by_label("Submit", exact=True).click()
            await page.wait_for_selector("//div[contains(text(),'Your response has been recorded.')]", timeout=20000)
            logger.info(f"Successfully submitted form for URL: {data['url']}")
        except Exception as e:
            logger.error("Error during form submission.", exc_info=True)

    except Exception as e:
        logger.error("Failed to fill out the Google Form.", exc_info=True)

# Define the missing function
async def login_and_get_context() -> Optional[BrowserContext]:
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            if await perform_login_and_otp(page):
                await context.storage_state(path=STORAGE_STATE)
                await browser.close()
                return context
            await browser.close()
    except Exception as e:
        logger.error("Failed to create browser context and perform login.", exc_info=True)
    return None

# Check if login is needed by attempting to access a URL from the CSV file
async def is_login_needed(test_url: str) -> bool:
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=STORAGE_STATE)
            page = await context.new_page()
            await page.goto(test_url)
            if "signin" in page.url or "ap/signin" in page.url:
                await browser.close()
                return True
            await browser.close()
            return False
    except Exception as e:
        logger.error("Failed to check login status.", exc_info=True)
        return True

# Process a single URL with a new Playwright context
async def process_url_with_new_context(url: str, semaphore: asyncio.Semaphore, retries: int):
    async with semaphore:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(storage_state=STORAGE_STATE)
                page = await context.new_page()
                extracted_data = await process_url(page, url, retries=retries)
                if extracted_data:
                    await fill_google_form(page, extracted_data)
                await browser.close()
        except Exception as e:
            logger.error(f"Error processing URL: {url}", exc_info=True)

# Main process for URLs with multiple workers and slight offset
async def process_urls(workers: int, delay: float, retries: int, progress_var, total_urls):
    try:
        if not urls_data:
            logger.critical("No URLs loaded from the CSV. Exiting...")
            return

        test_url = urls_data[0]  # Use the first URL from the CSV to check login status
        login_needed = await is_login_needed(test_url)
        if login_needed:
            context = await login_and_get_context()
            if not context:
                logger.critical('Login failed. Exiting process.')
                return
    except Exception as e:
        logger.critical("Failed during login check. Exiting...", exc_info=True)
        return

    semaphore = asyncio.Semaphore(workers)
    tasks: List[asyncio.Task] = []
    for i, url in enumerate(urls_data):
        if stop_event.is_set():
            logger.info("Stopping all tasks.")
            break
        logger.info(f"Starting task for URL {i + 1}/{total_urls}: {url}")
        tasks.append(asyncio.create_task(process_url_with_new_context(url, semaphore, retries)))
        if len(tasks) >= workers:
            await asyncio.gather(*tasks)
            tasks = []
        await asyncio.sleep(delay)  # Slight offset for each worker
        progress_var.set((i + 1) / total_urls * 100)

    if tasks:
        await asyncio.gather(*tasks)
    logger.info("All tasks completed.")
    progress_var.set(100)

# Schedule the process
async def schedule_task(workers: int, delay: float, retries: int, progress_var):
    total_urls = len(urls_data)
    while not stop_event.is_set():
        try:
            await process_urls(workers, delay, retries, progress_var, total_urls)
        except Exception as e:
            logger.error("Error in scheduled task.", exc_info=True)
        await asyncio.sleep(24 * 3600)

# Function to start the asyncio event loop in a new thread
def start_async_loop(workers, delay, retries, progress_var):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(schedule_task(workers, delay, retries, progress_var))

# Tkinter GUI setup
def start_gui():
    root = Tk()
    root.title("Mini Scraper Configuration")

    # Main frame
    main_frame = Frame(root)
    main_frame.pack(pady=10, padx=10)

    # Configuration file path
    Label(main_frame, text="Config File Path:").grid(row=0, column=0, sticky='e')
    config_path = StringVar(value='config.json')
    config_entry = Entry(main_frame, textvariable=config_path, width=50)
    config_entry.grid(row=0, column=1)
    Button(main_frame, text="Browse", command=lambda: config_path.set(filedialog.askopenfilename())).grid(row=0, column=2)
    Hovertip(config_entry, "Specify the path to the configuration file.")

    # URLs CSV file path
    Label(main_frame, text="URLs CSV Path:").grid(row=1, column=0, sticky='e')
    urls_path = StringVar(value='urls.csv')
    urls_entry = Entry(main_frame, textvariable=urls_path, width=50)
    urls_entry.grid(row=1, column=1)
    Button(main_frame, text="Browse", command=lambda: urls_path.set(filedialog.askopenfilename())).grid(row=1, column=2)
    Hovertip(urls_entry, "Specify the path to the CSV file containing URLs.")

    # Number of workers
    Label(main_frame, text="Workers:").grid(row=2, column=0, sticky='e')
    workers = IntVar(value=10)
    workers_entry = Entry(main_frame, textvariable=workers, width=10)
    workers_entry.grid(row=2, column=1, sticky='w')
    Hovertip(workers_entry, "Specify the number of workers (concurrent tasks).")

    # Delay between requests
    Label(main_frame, text="Delay (seconds):").grid(row=3, column=0, sticky='e')
    delay = StringVar(value="1.5")
    delay_entry = Entry(main_frame, textvariable=delay, width=10)
    delay_entry.grid(row=3, column=1, sticky='w')
    Hovertip(delay_entry, "Specify the delay between requests in seconds.")

    # Number of retries
    Label(main_frame, text="Retries:").grid(row=4, column=0, sticky='e')
    retries = IntVar(value=3)
    retries_entry = Entry(main_frame, textvariable=retries, width=10)
    retries_entry.grid(row=4, column=1, sticky='w')
    Hovertip(retries_entry, "Specify the number of retries for each request.")

    # Progress bar
    progress_var = IntVar()
    progress_bar = Progressbar(main_frame, orient='horizontal', length=400, mode='determinate', variable=progress_var)
    progress_bar.grid(row=5, column=0, columnspan=3, pady=10)

    # Status label
    status_label = Label(main_frame, text="Status: Ready", anchor='w')
    status_label.grid(row=6, column=0, columnspan=3, sticky='w')

    # Log window
    log_frame = Frame(root)
    log_frame.pack(pady=10, padx=10)

    log_text = Text(log_frame, wrap='word', state='disabled', height=20, width=100)
    log_text.pack(side='left', fill='both', expand=True)
    log_scroll = Scrollbar(log_frame, orient=VERTICAL, command=log_text.yview)
    log_scroll.pack(side='right', fill='y')
    log_text['yscrollcommand'] = log_scroll.set

    # Redirect logging to the Tkinter Text widget
    text_handler = TextHandler(log_text)
    text_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger.addHandler(text_handler)

    # Start button
    def start_scraper():
        try:
            config_file = config_path.get()
            urls_file = urls_path.get()
            num_workers = workers.get()
            request_delay = float(delay.get())
            num_retries = retries.get()

            if not config_file or not urls_file:
                messagebox.showerror("Error", "Please select both config file and URLs CSV.")
                return

            load_config(config_file)
            load_urls(urls_file)

            stop_event.clear()
            status_label.config(text="Status: Running")

            # Run the asyncio event loop in a new thread
            Thread(target=start_async_loop, args=(num_workers, request_delay, num_retries, progress_var), daemon=True).start()
        except Exception as e:
            logger.critical("Critical failure in running the scraper.", exc_info=True)
            messagebox.showerror("Error", str(e))

    Button(main_frame, text="Start", command=start_scraper).grid(row=7, column=0, pady=10)

    # Stop button
    def stop_scraper():
        stop_event.set()
        status_label.config(text="Status: Stopped")

    Button(main_frame, text="Stop", command=stop_scraper).grid(row=7, column=1, pady=10)

    # Save config button
    def save_configuration():
        config_file = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON files", "*.json")])
        if config_file:
            save_config(config_file)

    Button(main_frame, text="Save Config", command=save_configuration).grid(row=7, column=2, pady=10)

    root.mainloop()

# Run the Tkinter GUI
if __name__ == "__main__":
    start_gui()
