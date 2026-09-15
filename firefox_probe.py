#!/usr/bin/env python3
"""Диагностика обычного Firefox через Selenium; не запускает GPT-режимы."""
import argparse
from pathlib import Path
import shutil


PROFILE = Path.home() / 'snap/firefox/common/mb-profile'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--setup', action='store_true',
                        help='Оставить окно для ручного входа до нажатия Enter в терминале')
    parser.add_argument('--profile', type=Path, default=PROFILE)
    parser.add_argument('--firefox', default='/snap/firefox/current/usr/lib/firefox/firefox')
    parser.add_argument('--geckodriver', default=shutil.which('geckodriver'))
    args = parser.parse_args()
    from selenium import webdriver
    from selenium.common.exceptions import TimeoutException
    from selenium.webdriver.firefox.service import Service
    from selenium.webdriver.support.ui import WebDriverWait

    if not args.geckodriver or not Path(args.firefox).is_file():
        parser.error('Нужны Firefox и geckodriver; укажите пути соответствующими параметрами.')
    profile = args.profile.expanduser().resolve()
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    options = webdriver.FirefoxOptions()
    options.binary_location = args.firefox
    # -profile использует каталог непосредственно, без временного клонирования.
    options.add_argument('-profile')
    options.add_argument(str(profile))
    options.add_argument('-no-remote')
    options.accept_insecure_certs = False
    options.page_load_strategy = 'eager'
    print(f'Постоянный профиль: {profile}', flush=True)
    driver = webdriver.Firefox(options=options, service=Service(args.geckodriver))
    try:
        driver.set_page_load_timeout(45)
        try:
            driver.get('https://chatgpt.com/')
        except TimeoutException:
            print('Таймаут загрузки; проверяю доступный интерфейс.', flush=True)
        if args.setup:
            print('Войдите вручную в открытом Firefox. CAPTCHA и разрешения '
                  'обрабатываются только вами. Данные входа сюда не вводите.', flush=True)
            input('После завершения настройки нажмите Enter здесь, чтобы закрыть окно: ')
        # Первые кнопки принадлежат SSR-оболочке. На этом ноутбуке интерактивный
        # composer появляется значительно позже document.readyState=complete.
        try:
            WebDriverWait(driver, 120, poll_frequency=2).until(
                lambda d: any(e.is_displayed() for e in d.find_elements(
                    'css selector', '#prompt-textarea[contenteditable="true"][role="textbox"]'))
                and any(e.is_displayed() for e in d.find_elements(
                    'css selector', '[aria-label="Start Voice"], [aria-label="Start dictation"]')))
        except TimeoutException:
            print('Интерактивный composer с голосовыми элементами не подтверждён '
                  'за 120 секунд. Это не доказывает отсутствие Voice.', flush=True)
        # Только названия видимых элементов; без содержимого переписки и хранилищ.
        elements = [e for e in driver.find_elements(
            'css selector', 'form button, form [role], form [aria-label], '
            '[data-testid="accounts-profile-button"]') if e.is_displayed()]
        names = [e.accessible_name for e in elements]
        print('Видимые элементы composer и профиля:', names, flush=True)
        print('Атрибуты:', [dict(tag=e.tag_name, role=e.aria_role,
              aria=e.get_attribute('aria-label'), id=e.get_attribute('id'),
              testid=e.get_attribute('data-testid')) for e in elements], flush=True)
        login_names = [e.accessible_name for e in driver.find_elements(
            'css selector', 'button, [role="button"]') if e.is_displayed()]
        if any(n.strip().lower() in {'log in', 'sign up for free', 'войти'} for n in login_names):
            print('BLOCKED: страница предлагает вход. Voice и Plus не подтверждены.', flush=True)
            return 2
        print('Диагностика завершена. Вход, подписка и запуск Voice этим тестом '
              'не подтверждаются; требуется проверка конкретных элементов.', flush=True)
        return 0
    finally:
        # Закрывается только Firefox этой WebDriver-сессии.
        driver.quit()


if __name__ == '__main__':
    raise SystemExit(main())
