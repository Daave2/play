import asyncio
import json
import os
import re
import requests
import logging
from datetime import datetime, timedelta
from playwright.async_api import async_playwright, Page, BrowserContext, TimeoutError
from typing import Optional, List, Dict

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('mini_scraper')

# Load configuration from config.json
try:
    with open('config.json', 'r') as config_file:
        config = json.load(config_file)
        assert 'login_url' in config and 'form_url' in config and 'login_email' in config and 'login_password' in config, \
            "Missing required config parameters"
except Exception as e:
    logger.critical("Failed to load configuration. Exiting...", exc_info=True)
    raise SystemExit(e)

LOGIN_URL = config['login_url']
FORM_URL = "https://docs.google.com/forms/d/e/1FAIpQLSeOxv-LFTLy_so8UQPPZCvZfuCMQU0HOQS7y43YDYShaX5o6g/viewform"
STORAGE_STATE = 'state.json'
urls_data = []

# Performance metrics
performance_data = {
    'total_urls': 0,
    'processed_urls': 0,
    'failed_urls': [],
    'retry_attempts': 0,
    'times_taken': [],
    'form_times': [],
    'url_times': []
}

# Load default data
def load_default_data():
    global urls_data
    try:
        with open('urls.csv', 'r') as file:
            urls_data = [row.strip() for row in file.readlines()]
        if not urls_data:
            raise ValueError("URLs data is empty")
        performance_data['total_urls'] = len(urls_data)
        logger.info(f'{len(urls_data)} URLs loaded from urls.csv')
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

        otp_field = await page.wait_for_selector('input[type="tel"]', timeout=15000)
        if otp_field:
            await asyncio.sleep(10)
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
            await asyncio.sleep(5)

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

# Change date to today with retries
async def change_date_to_today(page: Page, retries: int = 3):
    today = datetime.today().strftime('%m/%d/%Y')
    for attempt in range(retries):
        try:
            await page.get_by_text("Customised").click()
            await page.get_by_text("Customised").click()
            date_locator = page.locator("#katal-id-9")
            await date_locator.click()
            await date_locator.fill(today)
            await date_locator.click(click_count=3)
            await date_locator.fill(today)
            await page.get_by_text("1-day trailing7-day trailing30-day trailingCustomised Export CSV").click()
            await page.locator("kat-dropdown:nth-child(2) > .kat-select-container > .select-header").click()
            
            # Use a more specific selector for the 9:00 PM option
            dropdown_options = page.locator(".option")
            await dropdown_options.locator('text="9:00 PM"').click()
            
            await page.get_by_role("button", name="Apply").click()

            # Retry logic for clicking on the dynamic element using XPath
            for col_attempt in range(retries):
                try:
                    await page.evaluate('''() => {
                        const xpath = "/html/body/div[1]/div[2]/div/div/div/div[2]/div[2]/kat-tabs/kat-tab[1]/div/div[2]/kat-table/kat-table-head/kat-table-row[2]/kat-table-cell[10]/div";
                        const element = document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                        if (element) {
                            element.click();
                        }
                    }''')
                    break  # If successful, break out of the loop
                except Exception as e:
                    if col_attempt < retries - 1:
                        logger.warning(f"Timeout clicking on the dynamic element, retrying {col_attempt + 1}/{retries}...")
                        await asyncio.sleep(2)  # Wait before retrying
                    else:
                        raise

            return True
        except Exception as e:
            logger.error(f"Failed to change the date to today on attempt {attempt + 1}/{retries}.", exc_info=True)
            if attempt < retries - 1:
                await asyncio.sleep(2)  # Wait before retrying
    return False




# Process URL and extract data
async def process_url(page: Page, url: str, retries=3) -> Optional[dict]:
    start_time = datetime.now()
    for attempt in range(retries):
        logger.info(f"Processing URL: {url}, attempt {attempt + 1}")
        try:
            await page.goto(url)
            if "signin" in page.url or "ap/signin" in page.url:
                logger.info(f"Sign-in required for {url}. Skipping this URL.")
                performance_data['failed_urls'].append(url)
                return None

            date_changed = await change_date_to_today(page)
            if not date_changed:
                logger.error(f"Failed to change date to today after {retries} attempts for {url}")
                continue

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
                    performance_data['retry_attempts'] += 1
                    continue

                availability_tab_xpath = '//span[@slot="label" and text()="Availability"]'
                try:
                    await page.click(availability_tab_xpath)
                    availability_table_xpath = '//*[@id="content"]/div/div[2]/div[2]/kat-tabs/kat-tab[3]/div/div[2]/kat-table/kat-table-head/kat-table-row[2]/kat-table-cell[7]/div'
                    availability_data = await wait_for_selector_with_retry(page, availability_table_xpath)
                    if availability_data:
                        availability_text = await availability_data.inner_text()
                        if availability_text:
                            data['availability'] = availability_text

                    logger.info(f"Successfully processed URL: {url}")
                    performance_data['processed_urls'] += 1
                    performance_data['url_times'].append((datetime.now() - start_time).total_seconds())
                    return data
                except Exception as e:
                    logger.error("Error extracting availability data.", exc_info=True)
                    performance_data['retry_attempts'] += 1
                    continue
            else:
                logger.warning(f"Insufficient row data for {url}")
            break
        except Exception as e:
            logger.error("Failed to process URL. Please check the website availability and layout.", exc_info=True)
            performance_data['retry_attempts'] += 1
    logger.info(f"Failed to process URL after {retries} attempts: {url}")
    performance_data['failed_urls'].append(url)
    return None

# Fill Google form with extracted data
async def fill_google_form(page: Page, data: dict):
    start_time = datetime.now()
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
            performance_data['form_times'].append((datetime.now() - start_time).total_seconds())
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
async def process_url_with_new_context(url: str, semaphore: asyncio.Semaphore):
    async with semaphore:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(storage_state=STORAGE_STATE)
                page = await context.new_page()
                extracted_data = await process_url(page, url)
                if extracted_data:
                    await fill_google_form(page, extracted_data)
                await browser.close()
        except Exception as e:
            logger.error(f"Error processing URL: {url}", exc_info=True)

# Main process for URLs with multiple workers and slight offset
async def process_urls(workers: int):
    try:
        load_default_data()
    except Exception as e:
        logger.critical("Failed to load URLs. Exiting...", exc_info=True)
        return

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
    total_urls = len(urls_data)
    for i, url in enumerate(urls_data):
        logger.info(f"Starting task for URL {i + 1}/{total_urls}: {url}")
        tasks.append(asyncio.create_task(process_url_with_new_context(url, semaphore)))
        if len(tasks) >= workers:
            await asyncio.gather(*tasks)
            tasks = []
        await asyncio.sleep(0.2)  # Slight offset for each worker

    if tasks:
        await asyncio.gather(*tasks)
    logger.info("All tasks completed.")

    # Print performance report
    print_performance_report()

# Schedule the process
async def schedule_task(workers: int):
    while True:
        try:
            await process_urls(workers)
        except Exception as e:
            logger.error("Error in scheduled task.", exc_info=True)
        await asyncio.sleep(24 * 3600)

# Print performance report
def print_performance_report():
    total_urls = performance_data['total_urls']
    processed_urls = performance_data['processed_urls']
    failed_urls = len(performance_data['failed_urls'])
    retry_attempts = performance_data['retry_attempts']
    total_time = sum(performance_data['url_times'] + performance_data['form_times'])
    avg_url_time = sum(performance_data['url_times']) / len(performance_data['url_times']) if performance_data['url_times'] else 0
    avg_form_time = sum(performance_data['form_times']) / len(performance_data['form_times']) if performance_data['form_times'] else 0

    logger.info("Performance Report:")
    logger.info(f"Total URLs: {total_urls}")
    logger.info(f"Processed URLs: {processed_urls}")
    logger.info(f"Failed URLs: {failed_urls}")
    logger.info(f"Retry Attempts: {retry_attempts}")
    logger.info(f"Total Time Taken: {total_time:.2f} seconds")
    logger.info(f"Average URL Processing Time: {avg_url_time:.2f} seconds")
    logger.info(f"Average Form Submission Time: {avg_form_time:.2f} seconds")

    if failed_urls > 0:
        logger.info("Failed URLs List:")
        for url in performance_data['failed_urls']:
            logger.info(url)

# Run the task with multiple workers
if __name__ == "__main__":
    try:
        asyncio.run(schedule_task(workers=20))
    except Exception as e:
        logger.critical("Critical failure in running the scheduler. Exiting...", exc_info=True)
        raise SystemExit(e)
