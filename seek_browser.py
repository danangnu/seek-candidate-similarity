"""Visible Chrome, user sign-in, bounded read-only candidate browsing."""
import hashlib
import json
import math
import re
import time
from urllib.parse import urlencode, urlsplit, parse_qs
from profiles import COUNT_XPATH, CARDS_XPATH, NEXT_XPATH, uuid_from_url, extract_candidate_profile, numeric_source_identity, norm, normalized_profile_content

BASE = 'https://au.employer.seek.com'
LOCATION_ROOT = '#location-filter-multiselect-autosuggest'


class BrowserReadError(ValueError):
    def __init__(self, message, diagnostics):
        super().__init__(message)
        self.diagnostics = diagnostics



class SeekBrowser:
    def __init__(self, config):
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.support.ui import WebDriverWait
        self.config = config
        self.search_location()  # Validate before launching Chrome.
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

    def search_location(self):
        location = self.config.get('search_location', 'Western Australia WA')
        if location is None:
            return None
        if not isinstance(location, str) or not location.strip() or len(location) > 120:
            raise ValueError('browser.search_location must be a location label or null for all Australia')
        return location.strip()

    def diagnostics(self, stage):
        # Do not serialize page HTML, browser exception strings, or query/service tokens.
        result = {'stage': stage, 'search_location': self.search_location()}
        try:
            parsed = urlsplit(self.driver.current_url)
            title = self.driver.title.lower()
            if 'just a moment' in title or 'verification' in title:
                kind = 'verification'
            elif parsed.hostname != 'au.employer.seek.com' or 'login' in parsed.path or 'signin' in parsed.path:
                kind = 'login_or_other_origin'
            elif parsed.path.startswith('/talentsearch/profiles/'):
                kind = 'profile'
            elif parsed.path.startswith('/talentsearch/keyword'):
                kind = 'search_results'
            else:
                kind = 'other_seek_page'
            result['page_kind'] = kind
        except Exception:
            result['page_kind'] = 'unavailable'
        issue = getattr(self, '_profile_validation_issue', None)
        if isinstance(issue, str):
            result['profile_validation_issue'] = issue
        return result

    def wait_for(self, callback, stage):
        try:
            return self.wait.until(callback)
        except Exception as ex:
            if type(ex).__name__ not in ('TimeoutException', 'TimeoutError'):
                raise
            self.ensure_claim()
            raise BrowserReadError('Timed out while '+stage+'. Check the visible Chrome page and browser_diagnostics in the report.',
                                   self.diagnostics(stage)) from None

    def navigate(self, url, stage):
        self.ensure_claim()
        try:
            self.driver.get(url)
        except Exception as ex:
            if type(ex).__name__ not in ('TimeoutException', 'TimeoutError'):
                raise
            self.ensure_claim()
            raise BrowserReadError('Chrome navigation timed out while '+stage+'.', self.diagnostics(stage)) from None

    def location_selected(self, label):
        self.ensure_claim()
        buttons = self.driver.find_elements('css selector', LOCATION_ROOT+" button[aria-label^='Clear ']")
        labels = [norm(b.get_attribute('aria-label')[6:]) for b in buttons if b.is_displayed()]
        # Require exactly this chip, not text in the autosuggest list or candidate cards.
        return labels == [norm(label)]

    def set_location_text(self, label, input_ready):
        # React-controlled autosuggest can lose individual send_keys characters
        # while rerendering. Use the native setter and a bubbling input event.
        script = """
            const field = arguments[0], value = arguments[1];
            if (!field || !field.isConnected || field.disabled || field.readOnly) return false;
            field.focus();
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
            setter.call(field, value);
            field.dispatchEvent(new Event('input', {bubbles: true}));
            return true;
        """
        stage = 'verifying the complete '+label+' input text'
        for attempt in range(3):
            self.pause('entering '+label)
            # Locate AFTER the delay; the previous input may have been replaced.
            field = self.wait_for(input_ready, 'locating the Location input')
            self.ensure_claim()
            try:
                changed = self.driver.execute_script(script, field, label)
            except Exception as ex:
                if type(ex).__name__ != 'StaleElementReferenceException':
                    raise
                changed = False
            if not changed:
                continue
            consecutive = 0
            def stable_text(_):
                nonlocal consecutive
                self.ensure_claim()
                try:
                    current = input_ready(None)
                    value = current.get_attribute('value') if current else None
                except Exception as ex:
                    if type(ex).__name__ != 'StaleElementReferenceException':
                        raise
                    value = None
                if value != label:
                    consecutive = 0
                    return False
                consecutive += 1
                return consecutive >= 3
            try:
                self.wait_for(stable_text, stage)
                return
            except BrowserReadError:
                if attempt < 2:
                    print('Location text did not stay complete; retrying entry.')
        raise BrowserReadError('Could not enter the complete '+label+
                               ' location. No search results were collected.', self.diagnostics(stage))

    def resolve_location(self, label):
        """Select the real autosuggest option and obtain SEEK's opaque locations value."""
        self.close_intro()
        def input_ready(_):
            self.ensure_claim()
            elements = self.driver.find_elements('css selector', LOCATION_ROOT+' input[role="combobox"]')
            visible = [e for e in elements if e.is_displayed() and e.is_enabled()]
            return visible[0] if len(visible) == 1 else False
        field = self.wait_for(input_ready, 'locating the Location filter')
        if not self.location_selected(label):
            # Clear previous location chips only within this filter.
            clears = self.driver.find_elements('id', 'location-filter-clear-multiselect-autosuggest-input')
            for button in clears:
                if button.is_displayed() and button.is_enabled():
                    self.pause('clearing the previous location filter')
                    button.click()
                    break
            # SEEK suggests the full region label from the shorter search text.
            query = 'Western Australia' if norm(label) == norm('Western Australia WA') else label
            self.set_location_text(query, input_ready)
            def option_ready(_):
                self.ensure_claim()
                current = input_ready(None)
                if not current:
                    return False
                menu_id = current.get_attribute('aria-controls')
                if not menu_id:
                    return False
                menus = self.driver.find_elements('id', menu_id)
                matches = [e for menu in menus for e in menu.find_elements('css selector', '[role="option"]')
                           if e.is_displayed() and e.is_enabled() and norm(e.text) == norm(label)]
                if len(matches) > 1:
                    raise ValueError('More than one exact Location option; select the intended region manually')
                return matches[0] if len(matches) == 1 else False
            self.wait_for(option_ready, 'finding the exact '+label+' option')
            self.pause('selecting '+label)
            # Suggestions may rerender during the delay; retrieve the current option.
            option = self.wait_for(option_ready, 'finding the current '+label+' option')
            self.ensure_claim()
            option.click()
        def applied(_):
            self.ensure_claim()
            if not self.location_selected(label):
                return False
            parsed = urlsplit(self.driver.current_url)
            if parsed.hostname != 'au.employer.seek.com' or parsed.path != '/talentsearch/keyword':
                return False
            locations = parse_qs(parsed.query).get('locations', [])
            return locations[0] if len(locations) == 1 and locations[0] else False
        return self.wait_for(applied, 'confirming the applied '+label+' filter')

    def ensure_claim(self):
        guard = getattr(self, 'claim_guard', None)
        if guard is not None:
            guard()
        schedule = getattr(self, 'schedule_guard', None)
        if schedule is not None:
            schedule(guard)

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
        self.navigate(BASE+'/talentsearch/keyword?market=AU', 'opening SEEK login')
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
        location = self.search_location()
        self._profile_validation_issue = None
        self.last_search_scan = {'search_location': location, 'location_verified': False, 'cards_checked': 0}
        query = {'searchQuery': name, 'market': 'AU', 'pageNumber': 1,
                 'salaryType': 'ANNUAL', 'minSalary': 0, 'salaryUnspecified': 'true',
                 'sortBy': 'LAST_UPDATED'}
        cached = getattr(self, '_resolved_location', None)
        if location and cached and cached[0] == location:
            query['locations'] = cached[1]
        self.pause('the next name search')
        self.navigate(BASE+'/talentsearch/keyword?'+urlencode(query), 'opening the name search')
        if location:
            if 'locations' not in query:
                query['locations'] = self.resolve_location(location)
                self._resolved_location = (location, query['locations'])
                # Reload using the confirmed query to discard results from before selection.
                self.pause('the location-filtered name search')
                self.navigate(BASE+'/talentsearch/keyword?'+urlencode(query), 'opening the location-filtered search')
            self.wait_for(lambda _: self.location_selected(location), 'verifying the '+location+' selection')
            self.last_search_scan['location_verified'] = True
        def results_ready(_):
            if location and not self.location_selected(location):
                return False
            return self.page_snapshot()
        page = self.wait_for(results_ready, 'loading search result cards')
        links, names, seen_pages = {}, {}, set()
        total = page['total']
        for number in range(self.config.get('max_search_pages', 5)):
            fingerprint = tuple(page['links'])
            if fingerprint in seen_pages:
                raise ValueError('Search pagination repeated a page')
            seen_pages.add(fingerprint)
            links.update(page['links'])
            names.update(page['names'])
            self.last_search_scan.update(total_results=total, pages_read=number+1, cards_checked=len(links))
            if len(links) > self.config.get('max_candidates', 100):
                return {}, False, 'Candidate limit reached; narrow/review this name manually'
            if len(links) > total:
                return {}, False, 'Results changed during pagination; retry later'
            if not page['next'] and len(links) == total:
                selected = {uid: url for uid, url in links.items() if norm(names[uid]) == norm(name)}
                self.last_search_scan.update({'name_filter': 'exact_normalized', 'total_results': total,
                    'cards_checked': len(links), 'profiles_selected': len(selected),
                    'skipped_different_names': len(links)-len(selected),
                    'cards': [{'uuid': uid, 'name': names[uid], 'selected': uid in selected} for uid in links]})
                print(f'Checked {len(links)} result cards; up to {len(selected)} exact-name profiles to compare; '
                      f'skipped {len(links)-len(selected)} different-name results.')
                return selected, True, 'All result-card names checked; only exact normalized names selected for full comparison'
            if number+1 == self.config.get('max_search_pages', 5):
                break
            self.pause('the next results page')
            self.ensure_claim()
            if page['next']:
                # Reacquire after the delay; React may have replaced the control.
                current = self.wait_for(results_ready, 'checking pagination before advancing')
                if current['total'] != total or tuple(current['links']) != fingerprint:
                    return {}, False, 'Results changed before pagination; retry later'
                next_control = current['next']
            else:
                next_control = []
            if next_control:
                next_control[0].click()
            else:
                # A full page can load before Next, or SEEK may change its markup.
                # Use the existing search route without dropping the confirmed filter.
                query['pageNumber'] = number+2
                self.last_search_scan['pagination_fallback'] = 'pageNumber'
                self.last_search_scan['requested_page'] = number+2
                print(f'No usable Next control; opening results page {number+2} by URL.')
                self.navigate(BASE+'/talentsearch/keyword?'+urlencode(query),
                              'opening the next numbered search results page')
            def changed(_):
                result = results_ready(None)
                return result if result and tuple(result['links']) != fingerprint else False
            page = self.wait_for(changed, 'loading the next search results page')
            if page['total'] != total:
                return {}, False, 'Results changed during pagination; retry later'
        return links, False, 'Search page limit reached; no mapping will be saved'

    def read_current_profile(self, expected_uuid=None, numeric_id=None):
        # A full navigation clears the previous DOM. Require stable content across
        # multiple polls so a partially loaded tab is not captured immediately.
        stable = {'content': None, 'since': 0.0}
        self._profile_validation_issue = None
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
            except (ValueError, StaleElementReferenceException) as ex:
                self._profile_validation_issue = str(ex) if type(ex) is ValueError else 'Profile changed while reading'
                stable['content'] = None
                return False
        return self.wait_for(ready, 'loading the complete numeric profile' if numeric_id is not None else 'loading the complete UUID profile')

    def numeric_profile(self, numeric_id):
        if type(numeric_id) is not int or numeric_id <= 0:
            raise ValueError('A positive numeric SEEK ID is required')
        url = BASE+'/talentsearch/profiles/'+str(numeric_id)
        self.pause('the live numeric-ID profile')
        self.navigate(url, 'opening the numeric profile')
        profile = self.read_current_profile(numeric_id=numeric_id)
        redirected_uuid = numeric_source_identity(self.driver.current_url, numeric_id)
        source = {'mode': 'live_numeric', 'numeric_seek_id': numeric_id,
                  'requested_url': url, 'redirected_uuid': redirected_uuid,
                  'profile_sha256': hashlib.sha256(json.dumps(profile, sort_keys=True,
                      ensure_ascii=False).encode()).hexdigest()}
        return profile, source

    def profile(self, uid, url):
        self.pause('the next UUID candidate profile')
        self.navigate(url, 'opening the UUID profile')
        return self.read_current_profile(expected_uuid=uid)

    def close(self):
        self.driver.quit()
