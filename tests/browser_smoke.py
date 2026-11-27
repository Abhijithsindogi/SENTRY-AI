"""Chromium checks against a separate local server and disposable database."""
import os
import re
from pathlib import Path
import socket
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright


class BrowserSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        cls.url = f'http://127.0.0.1:{port}'
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('SENTRY_', 'PLAYWRIGHT_'))}
        cls.db_path = Path(cls.tmp.name) / 'browser.sqlite3'
        env['SENTRY_DB_PATH'] = str(cls.db_path)
        process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'engine:app', '--host',
                                    '127.0.0.1', '--port', str(port), '--log-level', 'warning'], env=env)

        def stop():
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)

        cls.addClassCleanup(stop)
        for _ in range(80):
            try:
                with urlopen(cls.url + '/account', timeout=0.5):
                    break
            except OSError:
                if process.poll() is not None:
                    raise RuntimeError('Test server exited')
                time.sleep(.25)
        else:
            raise RuntimeError('Test server failed to start')

    def test_desktop_mobile_navigation_and_workflows(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='msedge', headless=True)
            errors = []
            context = browser.new_context(viewport={'width': 1440, 'height': 950},
                                          reduced_motion='reduce', accept_downloads=True)
            context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(self.url)
                          else route.abort())
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(self.url, wait_until='domcontentloaded')
            expect(page.locator('#historyChart button').first).to_be_visible(timeout=20000)
            for link in page.locator('.nav a[href^="#"]').all():
                target = link.get_attribute('href')
                self.assertEqual(page.locator(target).count(), 1, target)
            page.locator('#effects').click()
            expect(page.locator('#effects')).to_have_attribute('aria-pressed', 'false')
            page.locator('#effects').click()
            expect(page.locator('#effects')).to_have_attribute('aria-pressed', 'true')
            self.assertEqual(page.locator('.navlinks a').count(), 6)
            expect(page.locator('#wearableWorld [data-artifact=ring]')).to_have_count(1)
            expect(page.locator('#pairGallery .pair-card')).to_have_count(7)
            page.locator('#onboardNext').click()
            expect(page.locator('#onboardTitle')).to_contain_text('Patient or caregiver')
            page.locator('#onboardPrevious').click()
            expect(page.locator('#onboardingProgress')).to_have_attribute('value', '1')
            page.locator('#advancedSettings > summary').click()
            page.locator('#legacyConcept > summary').click()
            page.locator('#viewToggle').click()
            expect(page.locator('#viewToggle')).to_have_attribute('aria-pressed', 'true')
            page.locator('#viewToggle').click()
            for control in ('cyanMode', 'violetMode', 'prevMode', 'nextMode', 'sekyoRefresh',
                            'refreshBaselines', 'refreshReviews', 'replayRun'):
                page.locator('#' + control).click()
            expect(page.locator('#replayResult')).to_contain_text('held-out')
            page.locator('#historyChart button').first.click()
            expect(page.locator('#historyDetail')).to_contain_text('samples')
            page.locator('#historyFilter select[name=grouping]').select_option('month')
            page.locator('#historyFilter button').click()
            expect(page.locator('#historyChart button').first).to_be_visible()
            for kind in ('Csv', 'Pdf'):
                with page.expect_download() as download:
                    page.locator('#history' + kind).click()
                self.assertTrue(Path(download.value.path()).stat().st_size > 30)
                if kind == 'Pdf':
                    self.assertTrue(Path(download.value.path()).read_bytes().startswith(b'%PDF-'))
            date_from = page.locator('#historyFilter input[name=start]')
            date_until = page.locator('#historyFilter input[name=end]')
            date_from.fill('2099-01-01')
            date_until.fill('2099-01-31')
            page.locator('#historyFilter button').click()
            expect(page.locator('#historyStatus')).to_contain_text('No readings match')
            date_from.fill('')
            date_until.fill('')
            page.locator('#historyFilter button').click()
            expect(page.locator('#historyChart button').first).to_be_visible()
            fixture = b'record_date,metric,value,source\n2025-01-01,heart_rate_bpm,71,Fixture\n'
            page.locator('#historyFile').set_input_files({'name': 'fixture.csv', 'mimeType': 'text/csv', 'buffer': fixture})
            page.locator('#historyUpload button').click()
            expect(page.locator('#uploadStatus')).to_contain_text('Imported 1')
            page.locator('#historyUpload button').click()
            expect(page.locator('#uploadStatus')).to_contain_text('Skipped 1 duplicates')
            page.locator('#makeDemoReview').click()
            expect(page.locator('#reviewEvents')).to_contain_text('Simulated check-in')
            review = page.locator('#reviewEvents article').first
            review.get_by_label('Caregiver note').fill('Browser fixture')
            review.get_by_role('button', name='Save note').click()
            expect(page.locator('#reviewStatus')).to_contain_text('saved')
            review.get_by_role('button', name='Mark read').click()
            expect(review).to_contain_text('Read')
            review.get_by_role('button', name='Acknowledge').click()
            expect(review).to_contain_text('Acknowledged')
            page.once('dialog', lambda dialog: dialog.accept())
            page.locator('#resetDemoReviews').click()
            expect(page.locator('#reviewStatus')).to_contain_text('Deleted 1')
            page.locator('#profileForm').get_by_label('Display name').fill('Local browser fixture')
            page.get_by_label('Reduce motion').check()
            page.locator('#profileForm button').click()
            expect(page.locator('#accountStatus')).to_contain_text('saved')
            page.reload(wait_until='domcontentloaded')
            expect(page.locator('#profileForm').get_by_label('Display name')).to_have_value('Local browser fixture')
            expect(page.locator('html')).to_have_class(re.compile(r'\breduce-motion\b'))
            expect(page.locator('#historicalEvidence')).to_contain_text('5 experimental flags among 45', timeout=15000)
            expect(page.locator('#liveEvidence')).to_contain_text('waiting for data')
            page.locator('#diaryForm select[name=kind]').select_option('symptom')
            page.locator('#diaryForm input[name=title]').fill('Browser symptom fixture')
            page.locator('#diaryForm button').click()
            expect(page.locator('#diaryEntries')).to_contain_text('Browser symptom fixture')
            page.locator('#diaryFilter input[name=start]').fill('2099-01-01')
            page.locator('#diaryFilter button').click()
            expect(page.locator('#diaryStatus')).to_contain_text('No diary entries')
            page.locator('#diaryFilter input[name=start]').fill('')
            page.locator('#diaryFilter button').click()
            expect(page.locator('#diaryEntries')).to_contain_text('Browser symptom fixture')
            with page.expect_download() as diary_download:
                page.locator('a[href="/family/diary.pdf"]').click()
            self.assertTrue(Path(diary_download.value.path()).read_bytes().startswith(b'%PDF-'))
            page.locator('#familyDirectoryForm input[name=name]').fill('Browser directory fixture')
            page.locator('#familyDirectoryForm button').click()
            expect(page.locator('#familyMembers')).to_contain_text('Browser directory fixture')
            page.once('dialog', lambda dialog: dialog.accept())
            page.locator('#familyMembers').get_by_role('button', name='Remove').click()
            expect(page.locator('#familyDirectoryStatus')).to_contain_text('No family members')
            page.locator('#makeDemoReview').click()
            simulated = page.locator('#reviewEvents article').first
            simulated.get_by_role('button', name='Simulate family notifications').click()
            expect(page.locator('#reviewStatus')).to_contain_text('SIMULATION ONLY')
            deliveries = page.request.get(self.url + '/family/deliveries').json()
            self.assertEqual(sum(row.get('status') == 'simulated' for row in deliveries), 3)
            page.locator('#familyForm input[name=label]').fill('Browser fixture')
            page.locator('#familyForm button').click()
            expect(page.locator('#familyStatus')).to_contain_text('preferences saved')
            expect(page.locator('#research')).to_contain_text('87.43%')
            response = page.request.get(self.url + '/research/offline.pdf')
            self.assertEqual(response.status, 200)
            self.assertTrue(response.body().startswith(b'%PDF-'))
            page.locator('#displayLanguage').select_option('kn')
            expect(page.locator('#settingsHeading')).to_have_attribute('lang', 'kn')
            page.locator('#displayLanguage').select_option('en')
            expect(page.locator('#capabilityRows')).to_contain_text('RR intervals', timeout=15000)
            expect(page.locator('#capabilityRows')).to_contain_text('SpO')
            page.locator('#judgeNext').click()
            expect(page.locator('#judgeStepTitle')).to_contain_text('Wearable ecosystem')
            expect(page.locator('#judgeOpen')).to_have_attribute('href', '#wearableStory')
            page.locator('#judgePrevious').click()
            expect(page.locator('#judgeStepTitle')).to_contain_text('Care coordination')
            stamp = datetime.now(timezone.utc).isoformat()
            with closing(sqlite3.connect(self.db_path)) as con, con:
                con.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,NULL)',
                            ('browser-context-fixture', stamp, 'live', 'patient_001', 'sentry_wearable',
                             stamp, 'Isolated browser test fixture'))
            page.locator('#reviewFilter').select_option('live')
            card = page.locator('#reviewEvents article').filter(has_text='Isolated browser test fixture')
            card.get_by_role('button', name='Show assessment and patient context').click()
            expect(card).to_contain_text('Browser symptom fixture')
            page.locator('#architectureFlow button[data-stage=ai]').click()
            expect(page.locator('#architectureDetail')).to_contain_text('device and context')
            expect(page.locator('#architectureFlow button[data-stage=ai]')).to_have_attribute('aria-pressed', 'true')
            expect(page.locator('#research meter')).to_have_count(5)
            page.locator('#startDemo').click()
            expect(page.locator('#judgeStepTitle')).to_be_focused()
            for _ in range(5):
                page.locator('#judgeNext').click()
            page.locator('#judgeAction').click()
            expect(page.locator('#judgeCaseStatus')).to_contain_text('3 consecutive flagged windows')
            page.locator('#judgeNext').click()
            page.locator('#judgeAction').click()
            expect(page.locator('#judgeCaseStatus')).to_contain_text('backup SMS logged locally')
            page.locator('#judgeNext').click()
            page.locator('#judgeAction').click()
            expect(page.locator('#judgeCaseStatus')).to_contain_text('caregiver acknowledged')
            expect(page.locator('#notificationSummary')).to_contain_text('simulation')
            page.locator('#notificationDeliveries summary').click()
            expect(page.locator('#notificationDeliveryEntries')).to_contain_text('Simulation only')
            self.assertEqual(page.request.get(self.url + '/research/causal-followup.pdf').status, 200)
            page.set_viewport_size({'width': 390, 'height': 844})
            expect(page.locator('.navlinks')).to_be_visible()
            page.locator('.navlinks a[href="#settings"]').click()
            expect(page).to_have_url(self.url + '/#settings')
            self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            # UI scan and connect are exercised with a simulated device; no local radio is used.
            page.route('**/devices/scan', lambda route: route.fulfill(json=[
                {'address': 'fixture', 'name': 'Test band', 'advertises_heart_rate': True}
            ]))
            page.route('**/devices/connect', lambda route: route.fulfill(status=202, json={'status': 'queued'}))
            page.locator('#scanWearables').click()
            expect(page.locator('#deviceActionStatus')).to_contain_text('Found 1')
            page.locator('#wearableChoice').select_option('fixture')
            page.locator('#connectionForm button[type=submit], #connectionForm button.btn.primary').click()
            expect(page.locator('#deviceActionStatus')).to_contain_text('Connection started')
            page.locator('#stopWearable').click()
            expect(page.locator('#deviceActionStatus')).to_contain_text('stopped')
            self.assertEqual(errors, [])
            browser.close()

    def test_scroll_scenes_and_reduced_motion(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='msedge', headless=True)
            page = browser.new_page(viewport={'width': 1280, 'height': 900}, reduced_motion='no-preference')
            self.assertTrue(page.request.put(self.url + '/account/profiles/patient',
                                             data={'reduced_motion': False}).ok)
            page.goto(self.url, wait_until='domcontentloaded')
            world = page.locator('#wearableWorld')
            expect(world.locator('[data-artifact]')).to_have_count(8)
            world.hover(position={'x': 40, 'y': 40})
            self.assertNotEqual(world.evaluate("(e) => e.style.getPropertyValue('--world-ry')"), '')
            chapter = page.locator('#storyChapters [data-scene="6"]')
            chapter.evaluate("(e) => window.scrollTo({top: window.scrollY + e.getBoundingClientRect().top - innerHeight * .58, behavior: 'instant'})")
            expect(page.locator('#storyStage .ecosystem')).to_have_attribute('data-scene', '6')
            expect(page.locator('#storyStage .phone-call')).to_be_attached()
            page.emulate_media(reduced_motion='reduce')
            animation = page.locator('#storyStage .artifact-spin').first.evaluate("(e) => getComputedStyle(e).animationName")
            self.assertEqual(animation, 'none')
            browser.close()


if __name__ == '__main__':
    unittest.main()
