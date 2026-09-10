"""Visible Chrome, user sign-in, bounded read-only candidate browsing."""
import hashlib
import json
import math
import re
import time
from urllib.parse import urlencode
from profiles import COUNT_XPATH, CARDS_XPATH, NEXT_XPATH, uuid_from_url, extract_candidate_profile, numeric_source_identity, norm, normalized_profile_content

BASE = 'https://au.employer.seek.com'


class SeekBrowser:
    def __init__(self, config):
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.support.ui import WebDriverWait
        self.config = config
        self.delay_seconds = self.validate_delay(config.get("delay_seconds", 10))
        options = webdriver.ChromeOptions()
        options.add_argument('--start-maximized')
        kwargs = {'options': options}
        if config.get('chromedriver'):
            kwargs['service'] = Service(config['chromedriver'])
        self.driver = webdriver.Chrome(**kwargs)
        self.driver.set_page_load_timeout(90)
        self.wait = WebDriverWait(self.driver, config.get('wait_seconds', 45), poll_frequency=0.5)

    @staticmethod
    def validate_delay(value):
        if isinstance(value, bool):
            raise ValueError('browser.delay_seconds must be a number from 0 to 300')
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            raise ValueError('browser.delay_seconds must be a number from 0 to 300') from None
        if not math.isfinite(seconds) or not 0 <= seconds <= 300:
            raise ValueError('browser.delay_seconds must be a number from 0 to 300')
        return seconds

    def ensure_claim(self):
        guard = getattr(self, 'claim_guard', None)
        if guard is not None:
            guard()

    def pause(self, action):
        self.ensure_claim()
        seconds = self.delay_seconds
        if seconds:
            print(f'Waiting {seconds:g} seconds before {action}... (Ctrl+C to stop)')
            time.sleep(seconds)
        self.ensure_claim()

    def close_intro(self):
        self.ensure_claim()
        from selenium.common.exceptions import StaleElementReferenceException, ElementClickInterceptedException
        try:
            for button in self.driver.find_elements('css selector', "#intro-basic-search-dialog button[aria-label='Close']"):
                if button.is_displayed() and button.is_enabled():
                    button.click()
                    return
        except (StaleElementReferenceException, ElementClickInterceptedException):
            pass

    def login(self):
        self.ensure_claim()
        self.driver.get(BASE+'/talentsearch/keyword?market=AU')
        input('Sign in to SEEK in Chrome, complete any verification, then press Enter here. ')
        self.ensure_claim()
        if not self.driver.current_url.startswith(BASE+'/talentsearch/'):
            raise ValueError('Chrome is not on SEEK Talent Search. Complete sign-in first.')
        self.close_intro()

    def page_snapshot(self):
        self.ensure_claim()
        from selenium.common.exceptions import StaleElementReferenceException
        try:
            self.close_intro()
            counters = self.driver.find_elements('xpath', COUNT_XPATH)
            if len(counters) != 1 or not counters[0].is_displayed():
                return False
            match = re.fullmatch(r'([\d,]+) matching profiles?', counters[0].text.strip())
            if not match:
                return False
            total = int(match[1].replace(',', ''))
            cards = self.driver.find_elements('xpath', CARDS_XPATH)
            if total and not cards:
                return False
            links, names = {}, {}
            for card in cards:
                url = card.get_attribute('href')
                uid = uuid_from_url(url)
                if uid in links:
                    raise ValueError('Duplicate UUID card on results page')
                headings = card.find_elements('xpath', ".//*[@data-role='heading']")
                if len(headings) != 1 or not norm(headings[0].text):
                    return False  # Do not silently drop an unreadable candidate card.
                names[uid] = headings[0].text.strip()
                # serviceToken remains only in memory, never in reports or database.
                links[uid] = url
            if len(links) > total:
                return False
            next_buttons = [e for e in self.driver.find_elements('xpath', NEXT_XPATH)
                            if e.is_displayed() and e.is_enabled()]
            if len(next_buttons) > 1:
                raise ValueError('Ambiguous pagination control')
            return {'total': total, 'links': links, 'names': names, 'next': next_buttons}
        except StaleElementReferenceException:
            return False

    def search(self, name):
        self.last_search_scan = {}
        query = {'searchQuery': name, 'market': 'AU', 'pageNumber': 1,
                 'salaryType': 'ANNUAL', 'minSalary': 0, 'salaryUnspecified': 'true',
                 'sortBy': 'LAST_UPDATED'}
        self.pause('the next name search')
        self.driver.get(BASE+'/talentsearch/keyword?'+urlencode(query))
        page = self.wait.until(lambda _: self.page_snapshot())
        links, names, seen_pages = {}, {}, set()
        total = page['total']
        for number in range(self.config.get('max_search_pages', 5)):
            fingerprint = tuple(page['links'])
            if fingerprint in seen_pages:
                raise ValueError('Search pagination repeated a page')
            seen_pages.add(fingerprint)
            links.update(page['links'])
            names.update(page['names'])
            if len(links) > self.config.get('max_candidates', 100):
                return {}, False, 'Candidate limit reached; narrow/review this name manually'
            if not page['next']:
                if len(links) != total:
                    return {}, False, 'Result set changed or was incomplete'
                selected = {uid: url for uid, url in links.items() if norm(names[uid]) == norm(name)}
                self.last_search_scan = {'name_filter': 'exact_normalized', 'total_results': total,
                    'cards_checked': len(links), 'profiles_selected': len(selected),
                    'skipped_different_names': len(links)-len(selected),
                    'cards': [{'uuid': uid, 'name': names[uid], 'selected': uid in selected} for uid in links]}
                print(f'Checked {len(links)} result cards; up to {len(selected)} exact-name profiles to compare; '
                      f'skipped {len(links)-len(selected)} different-name results.')
                return selected, True, 'All result-card names checked; only exact normalized names selected for full comparison'
            if number+1 == self.config.get('max_search_pages', 5):
                break
            self.pause('the next results page')
            page['next'][0].click()
            def changed(_):
                result = self.page_snapshot()
                return result if result and tuple(result['links']) != fingerprint else False
            page = self.wait.until(changed)
            if page['total'] != total:
                return {}, False, 'Results changed during pagination; retry later'
        return links, False, 'Search page limit reached; no mapping will be saved'

    def read_current_profile(self, expected_uuid=None, numeric_id=None):
        # A full navigation clears the previous DOM. Require stable content across
        # multiple polls so a partially loaded tab is not captured immediately.
        stable = {'content': None, 'since': 0.0}
        def ready(_):
            self.ensure_claim()
            from selenium.common.exceptions import StaleElementReferenceException
            try:
                if numeric_id is not None:
                    numeric_source_identity(self.driver.current_url, numeric_id)
                elif uuid_from_url(self.driver.current_url) != expected_uuid:
                    return False
                self.close_intro()
                for tab in self.driver.find_elements('xpath', "//button[@role='tab' and normalize-space(.)='Profile']"):
                    if tab.is_displayed() and tab.get_attribute('aria-selected') != 'true':
                        self.pause('opening the Profile tab')
                        tab.click()
                        return False
                if self.driver.execute_script('return document.readyState') != 'complete':
                    return False
                if any(e.is_displayed() for e in self.driver.find_elements('css selector', '[aria-busy="true"]')):
                    stable['content'] = None
                    return False
                profile = extract_candidate_profile(self.driver.page_source)
                content = normalized_profile_content(profile)
                if content != stable['content']:
                    stable.update(content=content, since=time.monotonic())
                    return False
                return profile if time.monotonic() - stable['since'] >= 1.5 else False
            except (ValueError, StaleElementReferenceException):
                stable['content'] = None
                return False
        return self.wait.until(ready)

    def numeric_profile(self, numeric_id):
        if type(numeric_id) is not int or numeric_id <= 0:
            raise ValueError('A positive numeric SEEK ID is required')
        url = BASE+'/talentsearch/profiles/'+str(numeric_id)
        self.pause('the live numeric-ID profile')
        self.driver.get(url)
        profile = self.read_current_profile(numeric_id=numeric_id)
        redirected_uuid = numeric_source_identity(self.driver.current_url, numeric_id)
        source = {'mode': 'live_numeric', 'numeric_seek_id': numeric_id,
                  'requested_url': url, 'redirected_uuid': redirected_uuid,
                  'profile_sha256': hashlib.sha256(json.dumps(profile, sort_keys=True,
                      ensure_ascii=False).encode()).hexdigest()}
        return profile, source

    def profile(self, uid, url):
        self.pause('the next UUID candidate profile')
        self.driver.get(url)
        return self.read_current_profile(expected_uuid=uid)

    def close(self):
        self.driver.quit()
