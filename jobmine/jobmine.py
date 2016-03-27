import time

from jobmine import urls
from jobmine import ids
from jobmine.exceptions import LoginFailed, NoPreviousQuery

from concurrent import futures
from itertools import zip_longest
from bs4 import BeautifulSoup
from contextlib import contextmanager
from selenium import webdriver
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support.expected_conditions import staleness_of
from selenium.common.exceptions import NoSuchElementException


HTML_PARSER = 'html.parser'
JOBS_PER_THREAD = 10
NUM_THREADS = 10


def grouper(iterable, n, fillvalue=None):
    "Collect data into fixed-length chunks or blocks"
    # grouper('ABCDEFG', 3, 'x') --> ABC DEF Gxx"
    args = [iter(iterable)] * n
    return zip_longest(*args, fillvalue=fillvalue)

class JobMineQuery(object):

    def __init__(self, term, employer_name, job_title, disciplines, levels):
        self.term = term
        self.employer_name = employer_name
        self.job_title = job_title
        self.disciplines = disciplines
        self.levels = levels


class JobMine(object):

    def __init__(self, username, password):
        self.last_query = None
        self.results = []

        #self.browser = webdriver.PhantomJS('phantomjs')
        self.browser = webdriver.Firefox()

        self.authorized = False
        self.login(username, password) # on success sets authorized to True

    def __del__(self):
        self.browser.quit()

    def login(self, username, password):
        with self.wait_for_page_load():
            self.browser.get(urls.LOGIN)

        data = {'userid': username, 'pwd': password}
        self._find_eles_by_id_and_send(data)

        with self.wait_for_page_load():
            self.browser \
                .find_element_by_id(ids.LOGIN) \
                .find_element_by_xpath("//input[@type='submit'][@name='submit']") \
                .submit()

        try:
            login_err = self.browser.find_element_by_class_name('PSERRORTEXT').text
            raise LoginFailed(login_err)
        except NoSuchElementException:
            self.authorized = True
            self._cache_token_cookie()

    def _cache_token_cookie(self):
        self.token = next(cookie for cookie in self.browser.get_cookies() if cookie['name'] == 'PS_TOKEN')

    def find_jobs_with_last_query(self):
        if self.last_query is not None:
            return self.find_jobs_with_query(self.last_query)
        else:
            raise NoPreviousQuery('You have not made a query yet')

    def find_jobs(self, term=1165, employer_name="", job_title="",
                  disciplines=["ENG-Software", "MATH-Computer Science", "MATH-Computing & Financial Mgm"],
                  levels=['junior', 'intermdiate', 'senior']):
        jmquery = JobMineQuery(term, employer_name, job_title, disciplines, levels)
        return self.find_jobs_with_query(jmquery)

    def find_jobs_with_query(self, query):
        with self.wait_for_page_load():
            self.browser.get(urls.SEARCH)

        # inject search parameters into page
        self._set_disciplines(query)
        self._set_text_search_params(query)
        #self._set_levels(query)

        #time.sleep(0.5)

        # basically wait until search has been executed and
        # jobmine has reload the first job component
        with self.wait_for_element_stale(element_id=ids.FIRST_JOB):
            self.browser.find_element_by_id(ids.SEARCH_BUTTON).click()
            #time.sleep(2)

        job_ids = self.get_job_ids()

        grouped_job_ids = self._group_job_ids(job_ids)
        executor = futures.ThreadPoolExecutor(NUM_THREADS)
        job_futures = [executor.submit(self.scrape_details_for_jobs, group) for group in grouped_job_ids]


        #jobs = [self.scrape_job(job_id) for job_id in job_ids]

        # flatten that shit (i wish this was Scala :/ )
        jobs = [job_future for f in futures.as_completed(job_futures) for job_future in f.result()]

        # cache last query and results
        self.last_results = jobs
        self.last_query = query

        return jobs

    def get_job_ids(self):
        job_ids = []

        while True:
            soup = BeautifulSoup(self.browser.page_source, HTML_PARSER)
            job_spans = soup.findAll('span', id=lambda x: x and x.startswith(ids.JOB_ID_GENERIC))
            job_ids.extend([span.text for span in job_spans if span.text != u'\xa0'])

            # check if we are on the last page of search results
            try:
                with self.wait_for_element_stale(element_id=ids.FIRST_JOB):
                    self.browser.find_element_by_id(ids.NEXT_PAGE_BUTTON).click()
            except NoSuchElementException:
                break

        return job_ids

    def scrape_details_for_jobs(self, job_ids):
        # authorize temp browser based on main browser
        temp_browser = webdriver.Firefox() 
        temp_browser.get(urls.LOGIN)
        temp_browser.add_cookie(self.token)

        jobs = []

        for job_id in job_ids:
            if job_id is None:
                break

            with self.wait_for_page_load_with_browser(temp_browser):
                temp_browser.get(urls.JOB_PROFILE + job_id)

            soup = BeautifulSoup(temp_browser.page_source, HTML_PARSER)
            job_data = {
                'job_id':                 job_id,
                'posting_open_date':      soup.find(id = ids.POSTING_OPEN_DATE).text,
                'last_day_to_apply':      soup.find(id = ids.LAST_DAY_TO_APPLY).text,
                'employer_job_number':    soup.find(id = ids.EMPLOYER_JOB_NUMBER).text,
                'employer':               soup.find(id = ids.EMPLOYER).text,
                'job_title':              soup.find(id = ids.JOB_TITLE).text,
                'work_location':          soup.find(id = ids.WORK_LOCATION).text,
                'available_openings':     int(soup.find(id = ids.AVAILABLE_OPENINGS).text),
                'hiring_process_support': soup.find(id = ids.HIRING_PROCESS_SUPPORT).text,
                'work_term_support':      soup.find(id = ids.WORK_TERM_SUPPORT).text,
                'comments':               soup.find(id = ids.COMMENTS).text,
                'job_description':        soup.find(id = ids.JOB_DESCRIPTION).text
            }

            disciplines = soup.find(id = ids.DISCIPLINES).text
            disciplines_more = soup.find(id = ids.DISCIPLINES_MORE).text
            job_data['disciplines'] = (disciplines + ', ' + disciplines_more).split(', ')

            job_data['levels'] = soup.find(id = ids.LEVELS).text.split(', ')
            job_data['grades_required'] = soup.find(id = ids.GRADES).text == 'Required'

            jobs.append(job_data)

        temp_browser.quit()
        return jobs

    def _group_job_ids(self, job_ids):
        num_jobs = len(job_ids)
        if num_jobs > NUM_THREADS * JOBS_PER_THREAD:
            return grouper(job_ids, int(num_jobs / NUM_THREADS) + 1)
        else:
            return grouper(job_ids, JOBS_PER_THREAD)

    def _set_text_search_params(self, query):
        data = {
            'UW_CO_JOBSRCH_UW_CO_WT_SESSION': query.term,
            'UW_CO_JOBSRCH_UW_CO_EMPLYR_NAME': query.employer_name,
            'UW_CO_JOBSRCH_UW_CO_JOB_TITLE': query.job_title
        }
        self._find_eles_by_id_and_send(data)

    def _set_disciplines(self, query):
        discip_xpath = "//select[@name='UW_CO_JOBSRCH_UW_CO_ADV_DISCP%d']/option[text()='%s']"

        for i in range(len(query.disciplines)):
            self.browser.find_element_by_xpath(discip_xpath % (i + 1, query.disciplines[i])).click()

    def _set_levels(self, query):
        level_elements = {
            'junior': self.browser.find_element_by_id(ids.LEVEL_JUNIOR),
            'intermdiate': self.browser.find_element_by_id(ids.LEVEL_INTERMEDIATE),
            'senior': self.browser.find_element_by_id(ids.LEVEL_SENIOR)
        }

        for name, ele in level_elements.items():
            if (name in query.disciplines and not ele.is_selected()) or \
               (name not in query.disciplines and ele.is_selected()):
                ele.click()

    def _find_eles_by_id_and_send(self, data):
        for _id in data:
            ele = self.browser.find_element_by_id(_id)

            ele.clear()
            ele.send_keys(data[_id])

    @contextmanager
    def wait_for_page_load(self, timeout=10):
        old_page = self.browser.find_element_by_tag_name('html')
        yield
        WebDriverWait(self.browser, timeout).until(staleness_of(old_page))

    @contextmanager
    def wait_for_page_load_with_browser(self, browser, timeout=10):
        old_page = browser.find_element_by_tag_name('html')
        yield
        WebDriverWait(browser, timeout).until(staleness_of(old_page))

    @contextmanager
    def wait_for_element_stale(self, element_id, timeout=10):
        element = self.browser.find_element_by_id(element_id)
        yield
        WebDriverWait(self.browser, timeout).until(staleness_of(element))
