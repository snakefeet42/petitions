#!/usr/bin/python
# -*- coding: utf-8 -*-
import csv
import logging
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict
from urllib import request
from urllib.error import HTTPError

from bs4 import BeautifulSoup

DATA_DIR = 'data'
CSV_WHOLE = os.path.join(DATA_DIR, 'petition.csv')
CSV_CORRUPT = os.path.join(DATA_DIR, 'petition_corrupted.csv')
CSV_SAMPLE = os.path.join(DATA_DIR, 'petition_sampled.csv')
CSV_CORRUPT_SAMPLE = os.path.join(DATA_DIR, 'petition_corrupted_sampled.csv')
SAMPLE_RATE = 0.05
FIELDS_TO_CORRUPT = ['category', 'votes', 'start', 'end']

logging.basicConfig(level=logging.INFO)


def main():
    while True:
        try:
            run()
            break
        except:
            # 너무 많은 내용을 한 번에 가져가려고 하면 간혹 일시적으로 차단됨.
            # 5초 쉬었다가 다시 시도.
            print('Retrying after 5 seconds...')
            time.sleep(5)

    # 결측치 넣은 파일 생성
    generate_modified_file(CSV_WHOLE, CSV_CORRUPT, False, True)
    # 샘플링한 파일 생성
    generate_modified_file(CSV_WHOLE, CSV_SAMPLE, True, False)
    # 결측치 넣고 샘플링한 파일 생성
    generate_modified_file(CSV_WHOLE, CSV_CORRUPT_SAMPLE, True, True)


def run():
    # 데이터 저장 디렉터리 생성
    try:
        os.mkdir(DATA_DIR)
    except FileExistsError:
        pass

    # 추가로 만료된 청원을 수집하여 기존 CSV 파일에 덧붙이기
    latest_id = get_latest_article_id()
    next_id = get_latest_saved_article_id() + 1

    logging.info(
        f'From {next_id} to {latest_id}: '
        f'about {latest_id - next_id} articles to go...'
    )

    # 동시에 두 개씩 병렬로 처리. workers를 더 늘리면 더 자주 차단됨.
    with ThreadPoolExecutor(max_workers=2) as exe:
        for article in exe.map(fetch_article, range(next_id, latest_id)):
            if article is None:
                continue
            save_article(article)
            logging.info(
                f'{article["article_id"]} of {latest_id}: {article["title"]} '
                f'https://www1.president.go.kr/petitions/'
                f'{article["article_id"]}'
            )


def generate_modified_file(src, dst, sample, corrupt):
    """원본 파일을 샘플링하고 결측치 넣은 새 파일 생성"""

    # 랜덤 시드 고정. 매번 동일한 결과가 보장되도록.
    random.seed(0)
    with open(src, 'r') as fr:
        with open(dst, 'w') as fw:
            csvr = csv.DictReader(fr)
            csvw = csv.DictWriter(fw, csvr.fieldnames)

            csvw.writeheader()

            rows = csvr

            # 샘플링
            if sample:
                rows = (row for row in rows if random.random() <= SAMPLE_RATE)
            # 결측치 추가
            if corrupt:
                rows = (corrupt_row(row) for row in rows)

            csvw.writerows(rows)


def corrupt_row(row):
    # 범주가 '육아/교육'이고 투표수가 50건 초과이면 20% 확률로 투표수에 결측치 넣기
    category = row['category'] == '육아/교육'
    votes = int(row['votes']) > 50
    if category and votes and random.random() <= 0.2:
        row['votes'] = ''
    # 각 행마다 5% 확률로 특정 필드에 결측치 넣기
    if random.random() <= 0.05:
        key = random.choice(FIELDS_TO_CORRUPT)
        row[key] = ''
    return row


def get_latest_article_id() -> int:
    """만료된 청원 목록 페이지를 분석하여 가장 최근에 만료된 글번호를 가져오기"""
    html = fetch_html('https://www1.president.go.kr/petitions?only=finished')
    soup = BeautifulSoup(html, "html5lib")
    href = soup.select_one('.bl_body .bl_wrap .bl_subject a')['href']
    article_id = int(re.match(r'.+/petitions/(\d+).*', href).group(1))
    return article_id


def get_latest_saved_article_id() -> int:
    """이미 저장한 가장 최근 글번호를 가져오기. 저장된 글이 없으면 0을 반환"""
    # 글이 없으면 0
    if not os.path.isfile(CSV_WHOLE):
        return 0

    # 파일 끝 부분에서 몇 줄 읽어온 뒤 마지막 줄의 첫 칼럼(article_id) 반환
    with open(CSV_WHOLE, 'rb') as f:
        # 마지막 줄을 빠르게 찾기 위해 "거의" 끝 부분으로 이동
        f.seek(0, os.SEEK_END)
        f.seek(-min([f.tell(), 1024 * 100]), os.SEEK_CUR)

        # 마지막 줄에서 article id 추출
        last_line = f.readlines()[-1].decode('utf-8')
        article_id = int(last_line.split(',')[0])

        return article_id


def fetch_article(article_id: int) -> Dict[str, any] or None:
    """글번호에 해당하는 글의 HTML 텍스트를 가져와서 파싱. 해당 글이 없으면 None"""
    url = f'https://www1.president.go.kr/petitions/{article_id}'

    try:
        html = fetch_html(url)
    except ValueError:
        return None

    soup = BeautifulSoup(html, "html5lib")

    title = query(soup, '.petitionsView_title')
    votes = int(query(soup, '.petitionsView_count .counter').replace(',', ''))
    category = query(soup, '.petitionsView_info_list li:nth-of-type(1)')[4:]
    start = query(soup, '.petitionsView_info_list li:nth-of-type(2)')[4:]
    end = query(soup, '.petitionsView_info_list li:nth-of-type(3)')[4:]

    answered = query(soup, '.petitionsView_progress h4') == '브리핑'
    if answered:
        content_selector = '.petitionsView_write > div:nth-of-type(4)'
    else:
        content_selector = '.petitionsView_write > div:nth-of-type(2)'

    content = remove_whitespaces(query(soup, content_selector)) \
        .replace('\n', '\\n') \
        .replace('\t', '\\t')

    return {
        'article_id': article_id,
        'title': title,
        'votes': votes,
        'answered': 1 if answered else 0,
        'category': category,
        'start': start,
        'end': end,
        'content': content,
    }


def save_article(article: Dict[str, any]) -> None:
    """글을 CSV 형태로 저장한다"""
    cols = [
        'article_id', 'start', 'end', 'answered', 'votes', 'category', 'title',
        'content'
    ]

    # 파일이 없으면 새로 만들고 칼럼 이름 저장
    if not os.path.isfile(CSV_WHOLE):
        with open(CSV_WHOLE, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(cols)

    # 새로운 행 추가
    with open(CSV_WHOLE, 'a', newline='') as f:
        w = csv.writer(f)
        w.writerow(article[col] for col in cols)


def fetch_html(url: str) -> str:
    """웹에서 HTML 문서를 읽어서 반환"""
    try:
        with request.urlopen(url) as f:
            if f.getcode() != 200:
                raise ValueError(f'Invalid status code: {f.getcode()}')
            html = f.read().decode('utf-8')
            return html
    except HTTPError as e:
        if e.code == 404:
            raise ValueError(f'Not found: {url}')
        else:
            raise e


def query(soup: BeautifulSoup, selector: str) -> str:
    """CSS selector로 요소를 찾은 뒤 텍스트 컨텐츠를 반환"""
    return soup.select_one(selector).text


def remove_whitespaces(text: str) -> str:
    """본문 텍스트에서 불필요한 공백 문자들 제거"""
    lines = text.split('\n')
    lines = (l.strip() for l in lines)
    lines = (l for l in lines if len(l) > 0)
    return '\n'.join(lines)


if __name__ == '__main__':
    main()
