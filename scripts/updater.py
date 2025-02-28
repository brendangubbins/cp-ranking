import datetime
import json
import os
import time
import re
from typing import List, NamedTuple
from collections import defaultdict

import requests
from Crypto.Cipher import AES
from bs4 import BeautifulSoup

os.environ["CF_USERNAME"] = "cheetahbot"
os.environ["CF_PASSWORD"] = "bottings5"

START_DATE = datetime.datetime(2023, 1, 17)

contests = {}
divisions = {}

class Submission(NamedTuple):
    platform: str
    handle: str
    contest_id: str
    problem_id: str
    rating: int
    division: int
    submission_id: int
    time: int
    upsolved: bool


def get_codeforces(handle: str) -> List[Submission]:
    url = f"https://codeforces.com/api/contest.list?gym=false"
    response = requests.get(url)
    for contest in response.json()['result']:
        contest_id = contest['id']
        contests[contest_id] = contest['startTimeSeconds'] + contest['durationSeconds']
        contest_name = contest['name'].lower()
        if "div. 1" in contest_name:
            divisions[contest_id] = 1
        elif "div. 2" in contest_name:
            divisions[contest_id] = 2
        elif "div. 3" in contest_name:
            divisions[contest_id] = 3
        elif "div. 4" in contest_name:
            divisions[contest_id] = 4
        else:
            divisions[contest_id] = 2
        contests[contest['id']] = contest['startTimeSeconds'] + contest['durationSeconds']
        
    def validate(submissions):
        def f(submission):
            if submission['verdict'] != 'OK':
                return False
            if submission['creationTimeSeconds'] < START_DATE.timestamp():
                return False
            if not submission.get('contestId'):
                return False
            if not contests.get(submission['contestId']):
                return False
            if submission['creationTimeSeconds'] - contests[submission['contestId']] > 604800:
                return False
            if submission['author']['participantType'] not in {'CONTESTANT', 'OUT_OF_COMPETITION'}:
                return False
            return True
        return list(filter(f, submissions))

    def unique(submissions):
        res = list()
        solved = set()
        for s in submissions[::-1]:
            key = s['problem']['contestId'], s['problem']['index']
            if key not in solved:
                solved.add(key)
                res.append(s)
        return res[::-1]

    def transform(submissions):
        def f(submission) -> Submission:
            return Submission(
                handle=handle,
                platform="codeforces",
                contest_id=submission['problem']['contestId'],
                problem_id=submission['problem']['index'],
                rating=submission['problem']['rating'] if 'rating' in submission['problem'] else -1,
                division=divisions[submission['problem']['contestId']],
                submission_id=submission['id'],
                time=submission['creationTimeSeconds'],
                upsolved=submission['creationTimeSeconds'] > contests[submission['contestId']]
            )
        return list(map(f, submissions))

    url = f"https://codeforces.com/api/user.status?handle={handle}&from=1&count=100000"
    response = requests.get(url)
    if not response.json().get("result"):
        print(f"couldn't get results for {handle}")
        return []
    submissions = response.json()["result"]
    return transform(unique(validate(submissions)))


def get_atcoder(handle: str) -> List[Submission]:
    difficulties = requests.get(
        "https://kenkoooo.com/atcoder/resources/problem-models.json").json()
    contests = requests.get(
        "https://kenkoooo.com/atcoder/resources/contests.json").json()
    contests = {c['id']: c for c in contests}
    submissions = requests.get(
        f"https://kenkoooo.com/atcoder/atcoder-api/v3/user/submissions?user={handle}&from_second={int(START_DATE.timestamp())}").json()

    def validate(submissions):
        def f(submission):
            if submission['result'] != 'AC':
                return False
            contest = contests[submission['contest_id']]
            if submission['epoch_second'] > contest['start_epoch_second'] + contest['duration_second']:
                return False
            return True
        return list(filter(f, submissions))

    def unique(submissions):
        res = list()
        solved = set()
        for s in submissions[::-1]:
            key = s['problem_id']
            if key not in solved:
                solved.add(key)
                res.append(s)
        return res

    def transform(submissions):
        def f(submission) -> Submission:
            return Submission(
                handle=handle,
                platform="atcoder",
                contest_id=submission['contest_id'],
                problem_id=submission['problem_id'],
                rating=difficulties[submission['problem_id']]['difficulty'],
                time=submission['epoch_second'],
                submission_id=submission['id'],
            )
        return list(map(f, submissions))

    return transform(unique(validate(submissions)))


class CFLogin:
    BASE = "https://codeforces.com"
    service_url = f"{BASE}/enter"

    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password

    def __enter__(self):
        self.session = requests.session()
        dt = self.session.get(self.service_url).text

        if "Redirecting" in dt:
            rcpc = self.get_rcpc(dt)
            self.session.cookies.set(
                "rcpc", rcpc, domain="codeforces.com", path="/")
            link = re.findall(r'href="(.+?)"', dt)[0]
            dt = self.session.get(link).text

        raw_html = BeautifulSoup(dt, 'html.parser')
        csrf_token = raw_html.find_all(
            "span", {"class": "csrf-token"})[0]["data-csrf"]
        headers = {
            'X-Csrf-Token': csrf_token,
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/105.0.0.0 Safari/537.36'
        }
        payload = {
            'csrf_token': csrf_token,
            'action': 'enter',
            'handleOrEmail': self.username,
            'password': self.password,
        }
        self.session.post(self.service_url, data=payload, headers=headers)
        return self

    def __exit__(self, etype, value, traceback):
        soup = BeautifulSoup(self.session.get(self.BASE).text, 'html.parser')
        logout = soup.select_one("a[href*=logout]")
        if logout:
            href = logout["href"]
            assert isinstance(href, str)
            self.session.get(self.BASE + href)

    def get_rcpc(self, dt):
        matched = re.findall(r'toNumbers\("(.+?)"\)', dt)
        assert len(matched) == 3
        key, iv, text = matched
        block = AES.new(bytes.fromhex(key), AES.MODE_CBC, bytes.fromhex(iv))
        rcpc = block.decrypt(bytes.fromhex(text)).hex()
        return rcpc


def get_icpc(handles: List[str], contests):

    profile_str = "href=\"/profile/"
    team_str = "<td class=\\status-party-cell\""
    team_end_str = "</td>"
    verdict_str = "submissionverdict=\""
    problem_str = "a href=\""
    time_str = "<span class=\"format-time\" data-locale=\"en\">"
    start = "<div class=\"datatable\" " + \
        "style=\"background-color: #E1E1E1; padding-bottom: 3px;\">"

    def get_token(data, start, end):
        pos = data.find(start)
        data = data[pos + len(start):]
        pos = data.find(end)
        tok = data[:pos]
        data = data[pos:]
        return (data, tok)

    def get_usernames(team):
        usernames = []
        while profile_str in team:
            team, names = get_token(team, profile_str, "\"")
            usernames.append(names)
        return usernames

    all_handles = [item.lower() for sublist in handles for item in sublist]

    with CFLogin(os.environ["CF_USERNAME"], os.environ["CF_PASSWORD"]) as cf:
        submissions = list()
        for contest in contests:
            contest_name = contest["name"]
            contest_start = datetime.datetime.strptime(
                contest["start"], "%b/%d/%Y %H:%M")
            contest_end = datetime.datetime.strptime(
                contest["end"], "%b/%d/%Y %H:%M")

            solved = {}
            index = 1
            need_break = False
            while not need_break and index <= 50:
                submission_url = f"{cf.BASE}/{contest_name}/status?pageIndex={index}&order=BY_JUDGED_DESC"
                data = cf.session.get(submission_url).text
                soup = BeautifulSoup(data, 'html.parser')
                data = str(soup)
                data = data[data.find(start):]
                fetched_cnt = 0
                while data.find(profile_str) != -1:
                    data, tm = get_token(data, time_str, "<")
                    data, team = get_token(data, team_str, team_end_str)
                    usernames = get_usernames(team)
                    data, problem = get_token(data, problem_str, "\"")
                    data, verdict = get_token(data, verdict_str, "\"")
                    dt = datetime.datetime.strptime(tm, "%b/%d/%Y %H:%M")
                    fetched_cnt += 1
                    if dt < contest_start:
                        need_break = True
                        break
                    for uname in usernames:
                        if verdict == "OK" and uname.lower() in all_handles:
                            timestamp = int(datetime.datetime.timestamp(dt))
                            if (uname, problem) not in solved:
                                solved[(uname, problem)] = timestamp
                            elif timestamp < solved[(uname, problem)]:
                                solved[(uname, problem)] = timestamp
                index += 1
                time.sleep(1)
                print(
                    f"fetched total: {len(solved)} current page: {index}, {fetched_cnt}")
                if not fetched_cnt:
                    break
            for [uname, problem], timestamp in sorted(solved.items(), key=lambda x: x[1]):
                submissions.append(Submission(
                    handle=uname,
                    platform="icpc",
                    contest_id=contest_name,
                    problem_id=problem,
                    rating=int(timestamp <= contest_end.timestamp()),
                    time=timestamp,
                    submission_id=0,
                ))
            print(f"done {contest_name}")
        return submissions


def read_json(path):
    with open(path, "r") as f:
        return json.load(f)


def main():
    # read data from src/data/
    base_path = os.path.dirname(os.path.dirname(__file__))
    data_path = os.path.join(base_path, "src", "data")
    handles = read_json(os.path.join(data_path, "handles.json"))
    icpc_contests = read_json(os.path.join(data_path, "icpcs.json"))

    submissions = list()
    # handle icpc
    # print("starting handling icpc")
    # cf_handles = [handle["codeforces_handles"] for handle in handles]
    # submissions.extend(get_icpc(cf_handles, icpc_contests))
    # print(f"fetched {len(submissions)} submissions from icpc")
    print("starting handling codeforces and atcoder")
    for handle in handles:
        for cf_handle in handle["codeforces_handles"]:
            submissions.extend(get_codeforces(cf_handle))
        for ac_handle in handle["atcoder_handles"]:
            submissions.extend(get_atcoder(ac_handle))
        print(f"done {handle}")
        time.sleep(1)

    # transform submissions to json
    submissions = list(map(lambda x: x._asdict(), submissions))
    # write submissions to src/submissions.json
    with open(os.path.join(data_path, "submissions.json"), "w") as f:
        json.dump(submissions, f, indent=2)


if __name__ == "__main__":
    main()
