#!/usr/bin/env python3
# Watching for repositories on GitHub
# Written in 2016-2017 by Yu-Jie Lin
# This is free and unencumbered software released into the public domain.
#
# Anyone is free to copy, modify, publish, use, compile, sell, or distribute
# this software, either in source code form or as a compiled binary, for any
# purpose, commercial or non-commercial, and by any means.
#
# In jurisdictions that recognize copyright laws, the author or authors of this
# software dedicate any and all copyright interest in the software to the
# public domain. We make this dedication for the benefit of the public at large
# and to the detriment of our heirs and successors. We intend this dedication
# to be an overt act of relinquishment in perpetuity of all present and future
# rights to this software under copyright law.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL THE
# AUTHORS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
# WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#
# For more information, please refer to <http://unlicense.org/>

import argparse
import base64
import json
import logging
import re
import subprocess
import sys
import tty
from datetime import datetime, timedelta
from os import path
from time import sleep, time

import feedparser as fp
import requests

import termios

NAME = 'gh-watch.py'

TIMEOUT = 10
RETRY = 30

SEARCH_API_BASE = 'https://api.github.com/search/'
SEARCH_REPO_URL = SEARCH_API_BASE + 'repositories'
SEARCH_CODE_URL = SEARCH_API_BASE + 'code'
SEARCH_CODE_QS_DICT = {
  'q': 'license OR copying OR copyright OR "public domain"',
  'per_page': 1,
}
README_URL = 'https://api.github.com/repos/{full_name}/readme'
RSS_URL_BASE = 'http://github-trends.ryotarai.info/rss/github_trends_{}_{}.rss'
CGHP = 'r/coolgithubprojects'
REDDIT_URL = 'https://www.reddit.com/{}/new/.json'
REDDIT_RES = [
  re.compile(RE)
  for RE in (
    r'https?://github\.com/(?P<user>[0-9a-zA-Z-]+)/(?P<repo>[0-9a-zA-Z-]+).*',
    r'https?://(?P<user>[0-9a-zA-Z-]+)\.github\.io/(?P<repo>[0-9a-zA-Z-]+)/.*',
  )
]

log_fmt = '[%(asctime)s][%(levelname)7s] %(message)s'
logging.basicConfig(format=log_fmt)
log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


# http://code.activestate.com/recipes/134892/
def getch():

  fd = sys.stdin.fileno()
  old_settings = termios.tcgetattr(fd)
  try:
      tty.setraw(sys.stdin.fileno())
      ch = sys.stdin.read(1)
  finally:
      termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
  return ch


def print_repo(r):

  print('https://github.com/', end='')
  print('\033[32m{user}\033[0m/\033[33m{repo}\033[0m'.format(**r))
  if r['type'] == 'search':
    fmt = '{language:20s} {stargazers_count:6,} / {forks_count:6,}'
    print(fmt.format(**r))
    if r['homepage']:
      print(r['homepage'])
  elif r['type'] == 'trend':
    print('{language:20s} Trending'.format(**r))
  elif r['type'] == 'r':
    if r['sub'] == 'r/coolgithubproject':
      print('{language:20s} Score = {score:7,}'.format(**r))
    else:
      print('{sub:20s} Score = {score:7,}'.format(**r))
    print(r['thread'])
  print()

  if r['description']:
    print(r['description'])
    print()


def check_license(r, cache):

  fn = r['full_name']
  d = SEARCH_CODE_QS_DICT.copy()
  d['q'] += ' repo:{}'.format(fn)

  log.debug('checking {} for license...'.format(fn))
  resp = cache.gh_req(SEARCH_CODE_URL, params=d, timeout=TIMEOUT)
  return resp.json()['total_count'] > 0


def filter_repo(r, config):

  for f in config.filters_user:
    if f.search(r['user']):
      msg = '{} username matched /{}/, skipped'
      log.debug(msg.format(r['user'], f.pattern))
      return True

  for f in config.filters_repo + config.filters_repo_desc:
    if f.search(r['repo']):
      msg = '{} repo name matched /{}/, skipped'
      log.debug(msg.format(r['full_name'], f.pattern))
      return True

  for f in config.filters_description + config.filters_repo_desc:
    if r['description'] is None:
      continue
    if f.search(r['description']):
      msg = '{} description matched /{}/, skipped'
      log.debug(msg.format(r['full_name'], f.pattern))
      return True


class Data():

  PATH = '/tmp'
  DICT = {}

  def __init__(self):

    filename = '{}.{}.json'.format(NAME, self.__class__.__name__.lower())
    self.JSON = path.join(self.PATH, filename)

    self.data = self.DICT.copy()
    if path.exists(self.JSON):
      log.debug('loading {}...'.format(self.JSON))
      with open(self.JSON) as f:
        self.data.update(json.load(f))

    self.updated = False

  def __del__(self):

    self.save()

  def __len__(self):

    return len(self.data)

  def __getitem__(self, key):

    return self.data[key]

  def __setitem__(self, key, value):

    self.data[key] = value
    self.updated = True

  def __delitem__(self, key):

    del self.data[key]
    self.updated = True

  def __iter__(self):

    return self.data.__iter__()

  def __contains__(self, item):

    return item in self.data

  def keys(self):

    return self.data.keys()

  def values(self):

    return self.data.values()

  def get(self, key, *args):

    return self.data.get(key, *args)

  def save(self):

    if not self.updated:
      return

    with open(self.JSON, 'w') as f:
      json.dump(self.data, f)
      self.updated = False

    log.info('{} saved.'.format(self.JSON))


class Config(Data):

  PATH = path.expanduser('~/.config')
  DICT = {
    'cmd_readme': 'less',
    'accept_languages': ['All'],
    'filters_user': [],
    'filters_repo_desc': [],
    'filters_repo': [],
    'filters_description': [],
    'snooze_seconds': 7 * 86400,
  }

  def __init__(self):

    super(self.__class__, self).__init__()

    # compile filters
    self.filters_user = []
    for f in self['filters_user']:
      self.filters_user.append(re.compile(f))

    self.filters_repo_desc = []
    for f in self['filters_repo_desc']:
      self.filters_repo_desc.append(re.compile(f))

    self.filters_repo = []
    for f in self['filters_repo']:
      self.filters_repo.append(re.compile(f))

    self.filters_description = []
    for f in self['filters_description']:
      self.filters_description.append(re.compile(f))


class Repos(Data):

  PATH = path.expanduser('~/.local/share')
  DICT = {
    'snooze': {},
    'zap': [],
  }

  def __init__(self, config):

    super(self.__class__, self).__init__()

    self.config = config

    # clean up snoozed repos
    threshold = time() - self.config['snooze_seconds']
    del_count = 0
    for r in list(self['snooze'].keys()):
      if threshold >= self['snooze'][r]:
        self.updated = True
        del self['snooze'][r]
        del_count += 1

    if del_count:
      log.info('{:,} snoozed repos expired.'.format(del_count))

  def __contains__(self, item):

    return item in self['snooze'] or item in self['zap']

  def snooze(self, full_name):

    self['snooze'][full_name] = time()
    self.updated = True

  def zap(self, full_name):

    self['zap'].append(full_name)
    self.updated = True

  def remove_zap(self, full_name):

    self['zap'].remove(full_name)
    self.updated = True


class Cache(Data):

  DICT = {
    'repos': {},
    'fetches': {},
  }

  def __init__(self, config, repos):

    super(self.__class__, self).__init__()

    self.config = config
    self.repos = repos

  def fetch(self):

    for fetch in self.config['fetches']:
      t = self['fetches'].get(fetch['key'], None)
      threshold = time() - fetch['interval']
      if t is not None and threshold < t:
        continue

      if fetch['type'] == 'search':
        self.fetch_search(fetch)
      elif fetch['type'] == 'trend':
        self.fetch_trend(fetch)
      elif fetch['type'] == 'r':
        self.fetch_r(fetch)
      else:
        log.error('unknown fetch: {}'.format(fetch['key']))
        continue

      self['fetches'][fetch['key']] = time()
      self.updated = True

  def gh_req(self, URL, raise_error=True, **kwds):

    rl_type = 'search' if '/search/' in URL else 'general'
    rl_type = 'api_rl_' + rl_type
    rl = self.get(rl_type, {'remain': 1, 'reset': 0})

    if rl['remain'] == 0 and time() < rl['reset']:
      s = rl['reset'] - time()
      t = datetime.now() + timedelta(seconds=s)
      fmt = 'sleeping until {} ({:.0f} seconds) for ratelimit reset...'
      log.info(fmt.format(t.strftime('%H:%M:%S'), s))
      sleep(s)

    r = requests.get(URL, **kwds)
    rl['remain'] = int(r.headers['X-RateLimit-Remaining'])
    rl['reset'] = int(r.headers['X-RateLimit-Reset'])
    self[rl_type] = rl
    msg = 'GitHub API {}: {} requests remained, reset in {} seconds'
    log.debug(msg.format(rl_type, rl['remain'], int(rl['reset'] - time())))
    if raise_error:
      r.raise_for_status()
    return r

  def fetch_search(self, fetch):

    d = {
      'q': fetch['q'],
      'sort': fetch.get('sort', 'best'),
      'per_page': fetch.get('per_page', 100),
    }

    log.info('searching for [{}]...'.format(d['q']))
    resp = self.gh_req(SEARCH_REPO_URL, params=d).json()
    log.debug('{:,} repositories matched in total'.format(resp['total_count']))
    log.debug('{} repositories returned.'.format(len(resp['items'])))

    langs = self.config['accept_languages']
    for r in resp['items']:
      fn = r['full_name']
      lang = r['language']
      repo = {
        'type': 'search',
        'full_name': fn,
        'user': r['owner']['login'],
        'repo': r['name'],
        'language': lang,
        'stargazers_count': r['stargazers_count'],
        'forks_count': r['forks_count'],
        'html_url': r['html_url'],
        'homepage': r['homepage'],
        'description': r['description'],
      }

      if filter_repo(repo, self.config):
        continue
      if fn in self['repos'] or fn in self.repos:
        continue
      if 'All' not in langs and lang not in langs:
        continue

      log.debug('adding {} to cache...'.format(fn))
      self['repos'][fn] = repo
      self.updated = True

  def fetch_trend(self, fetch):

    langs = fetch['languages']
    if langs == 'accept_languages':
      langs = self.config['accept_languages']
    for lang in langs:
      if lang == '':
        lang = 'Unknown'
      lang = lang.lower().replace('c++', 'cpp')
      self.fetch_trend_lang(lang, fetch['period'])

  def fetch_trend_lang(self, lang, period):

    RSS_URL = RSS_URL_BASE.format(lang, period)
    log.debug('reguesting {}...'.format(RSS_URL))
    f = fp.parse(RSS_URL)
    log.debug('{} repositories returned.'.format(len(f.entries)))

    for r in f.entries:
      fn, lang = r.title.split(' ', 1)
      desc = r.description.rstrip('\n') if hasattr(r, 'description') else ''
      lang = lang.split(' - ')[1]
      user, repo = fn.split('/')
      repo = {
        'type': 'trend',
        'full_name': fn,
        'user': user,
        'repo': repo,
        'language': lang,
        'html_url': r.link,
        'description': desc,
      }

      if filter_repo(repo, self.config):
        continue
      if fn in self['repos'] or fn in self.repos:
        continue

      log.debug('adding {} to cache...'.format(fn))
      self['repos'][fn] = repo
      self.updated = True

  def fetch_r(self, fetch):

    log.info('searching in {}...'.format(fetch['key']))
    while True:
      try:
        r = requests.get(REDDIT_URL.format(fetch['key']))
        log.debug('{} received.'.format(r.url))
        resp = r.json()
        if 'error' not in resp:
          break
        log.error('{error}: {message}'.format(**resp))
        log.error('retry in {} seconds...'.format(RETRY))
        sleep(RETRY)
      except (requests.HTTPError, requests.Timeout) as e:
        log.error(repr(e))
        log.error('retry in {} seconds...'.format(RETRY))
        sleep(RETRY)
    resp = resp['data']['children']
    log.debug('{} repositories returned.'.format(len(resp)))

    langs = self.config['accept_languages']
    for r in resp:
      r = r['data']
      for RE in REDDIT_RES:
        m = RE.match(r['url'])
        if m:
          break
      else:
        # only give warnings on r/coolgithubprojects
        if fetch['key'] == CGHP:
          log.warning('{} not matched the pattern, skipped.'.format(r['url']))
        continue
      user, repo = m.group('user'), m.group('repo')
      fn = '{}/{}'.format(user, repo)
      lang = 'Unknown'
      if fetch == CGHP:
        lang = r['link_flair_text'].replace('CPP', 'C++').title()
      repo = {
        'type': 'r',
        'sub': fetch['key'],
        'full_name': fn,
        'user': user,
        'repo': repo,
        'language': lang,
        'score': r['score'],
        'html_url': r['url'],
        'thread': 'https://www.reddit.com' + r['permalink'],
        'description': r['title'],
      }

      if filter_repo(repo, self.config):
        continue
      if fn in self['repos'] or fn in self.repos:
        continue
      if fetch['key'] == CGHP \
         and 'All' not in langs and lang not in langs:
        continue

      log.debug('adding {} to cache...'.format(fn))
      self['repos'][fn] = repo
      self.updated = True


def recheck(config, repos):

  log.info('rechecking filters against repositories...')
  log.info('rechecking with filters_repo and filters_repo_desc...')
  removals = 0
  for user_repo in repos['zap'][:]:
    user, repo = user_repo.split('/')
    for f in config.filters_user:
      if not f.search(user):
        continue
      msg = '{} username matched /{}/, removed'
      log.info(msg.format(user, f.pattern))
      repos.remove_zap(user_repo)
      removals += 1
    for f in config.filters_repo + config.filters_repo_desc:
      if not f.search(repo):
        continue
      msg = '{} repo name matched /{}/, removed'
      log.info(msg.format(repo, f.pattern))
      repos.remove_zap(user_repo)
      removals += 1
  log.info('{} repos removed'.format(removals))


def analyze(repos):

  from collections import Counter

  data = {}
  for key in ('usernames', 'reponames', 'words'):
    data[key] = Counter()

  RE_WORD = re.compile('[-.]')
  for user_repo in repos['zap']:
    user, repo = user_repo.split('/')
    data['usernames'].update((user, ))
    data['reponames'].update((repo, ))
    data['words'].update((r for r in RE_WORD.split(repo) if r))

  log.info('{:6,d} usernames'.format(len(data['usernames'])))
  log.info('{:6,d} repositories'.format(len(repos['zap'])))

  TOP = 20
  for k, v in data.items():
    log.info('most zapped {}:'.format(k))
    for w, c in v.most_common(TOP):
      log.info('{:3d}: {}'.format(c, w))


def main():

  p = argparse.ArgumentParser()
  p.add_argument('--debug', '-d', action='store_true',
                 help='print out debugging messages')
  p.add_argument('--force', '-f', action='store_true',
                 help='force fetching ones have reached interval time')
  p.add_argument('--force-all', '-F', action='store_true',
                 help='force fetching all')
  p.add_argument('--check', '-c', action='store_true',
                 help='check licenses at once, non-interactive')
  p.add_argument('--recheck', '-r', action='store_true',
                 help='recheck filters against repositories')
  p.add_argument('--analyze', '-a', action='store_true',
                 help='provide some statistics')
  args = p.parse_args()

  if args.debug:
    log.setLevel(logging.DEBUG)

  config = Config()
  repos = Repos(config)
  cache = Cache(config, repos)

  if args.recheck:
    recheck(config, repos)
    return
  elif args.analyze:
    analyze(repos)
    return
  elif args.force:
    log.info('forcing fetching...')
    cache.fetch()
  elif args.force_all:
    log.info('forcing fetching all...')
    cache['fetches'].clear()
    cache.fetch()
  elif not cache['repos']:
    log.info('no cached repos, fetching...')
    cache.fetch()

  c = len(cache['repos'])
  w = len(str(c))
  for i, fn in enumerate(list(sorted(cache['repos'].keys())), start=1):
    r = cache['repos'][fn]
    if filter_repo(r, config):
      del cache['repos'][fn]
      cache.updated = True
      continue

    if 'license' not in r:
      r['license'] = None
      skip = False
      while True:
        try:
          r['license'] = check_license(r, cache)
          cache.updated = True
          break
        except (requests.HTTPError, requests.Timeout) as e:
          msg = repr(e)
          log.error(msg)
          if '422 Client Error' in msg:
            log.error('skipping...'.format(RETRY))
            skip = True
            break
          log.error('retry in {} seconds...'.format(RETRY))
          sleep(RETRY)

      if skip:
        continue

      if not r['license']:
        repos.snooze(fn)
        del cache['repos'][fn]
        cache.updated = True
        log.info('no possible license found in {}, auto-snoozed.'.format(fn))
        continue

    print('[{:{w}}/{}] '.format(i, c, w=w), end='')
    print_repo(r)
    while True and not args.check:
      print('[z]ap [s]nooze [r]eadme [c]heck ', end='')
      if r['type'] == 'search':
        print('[h]omepage ', end='')
      elif r['type'] == 'cghp':
        print('[t]thread ', end='')
      print('[space] skip [q]uit? ', end='')
      sys.stdout.flush()
      ans = getch()
      print(ans)
      if ans == 'z':
        repos.zap(fn)
        del cache['repos'][fn]
        cache.updated = True
        break
      elif ans == 's':
        repos.snooze(fn)
        del cache['repos'][fn]
        cache.updated = True
        break
      elif ans == 'r':
        API_URL = README_URL.format(**r)
        headers = {'Accept': 'application/vnd.github.v3.object'}
        resp = cache.gh_req(API_URL, raise_error=False, headers=headers)
        if resp.status_code == requests.codes.not_found:
          log.info('{} does not have an README.'.format(fn))
          continue
        resp.raise_for_status()
        j = resp.json()
        if j['encoding'] == 'base64':
          text = base64.b64decode(j['content'])
        else:
          log.error('unable to handle {} encoding'.format(j['encoding']))
          continue
        if j['name'].lower() == 'readme.md':
          cmd = config.get('cmd_readme_md', config['cmd_readme'])
        else:
          cmd = config['cmd_readme']
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, shell=True)
        p.communicate(input=text)
      elif ans == 'c':
        cmd = config['cmd_url']
        subprocess.Popen(cmd.format(r['html_url']), shell=True)
      elif ans == 'h' and r['type'] == 'search' and r['homepage']:
        cmd = config['cmd_url']
        subprocess.Popen(cmd.format(r['homepage']), shell=True)
      elif ans == 't' and r['type'] == 'cghp' and r['thread']:
        cmd = config['cmd_url']
        subprocess.Popen(cmd.format(r['thread']), shell=True)
      elif ans == ' ':
        break
      elif ans == 'q':
        cache.save()
        repos.save()
        return
    if not args.check:
      print()
  cache.save()
  repos.save()


if __name__ == '__main__':
  main()
