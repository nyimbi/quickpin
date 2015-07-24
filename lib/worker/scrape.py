''' Worker functions for performing scraping tasks asynchronously. '''

import bs4
from datetime import datetime
import hashlib
import json
import pickle
from urllib.error import HTTPError
import urllib.parse

import requests
from sqlalchemy.exc import IntegrityError

import app.database
import app.index
import app.queue
from model import File, Profile
import worker
import worker.index


def scrape_account(site, name):
    ''' Scrape a twitter account. '''

    redis = worker.get_redis()

    account_scrapers = {
        'twitter': _scrape_twitter_account,
    }

    session = worker.get_session()

    if site in account_scrapers:
        try:
            profile = account_scrapers[site](name)
            redis.publish('profile', json.dumps(profile))
        except HTTPError as he:
            message = {'name': name, 'site': site, 'error_code': he.code}
            if he.code == 404:
                message['error'] = 'Profile not found.'
            else:
                message['error'] = 'Error while communicating with {}.'.format(site)
            redis.publish('profile', json.dumps(message))
        except Exception as e:
            message = {
                'name': name,
                'site': site,
                'error': 'Unknown error while fetching profile.'
            }
            redis.publish('profile', json.dumps(message))
            raise

    else:
        raise ValueError('No scraper exists for site "{}"'.format(site))


def _scrape_twitter_account(username):
    ''' Scrape twitter bio data and create (or update) a profile. '''

    db_session = worker.get_session()
    twitter_session = _login_twitter()
    twitter_url = 'https://twitter.com'
    home_url = '{}/{}'.format(twitter_url, username)
    response = twitter_session.get(home_url)

    if response.status_code != 200:
        message = 'Not able to get home page for "{}" ({})' \
                  .format(username, response.status_code)
        raise HTTPError(code=response.status_code, message=message)

    html = bs4.BeautifulSoup(response.text, 'html.parser')

    # Get Twitter ID and upsert the profile.
    profile_el = html.select('.ProfileNav-item--userActions .user-actions')[0]
    user_id = profile_el['data-user-id']
    profile = Profile('twitter', user_id, username)
    db_session.add(profile)

    try:
        db_session.commit()
    except IntegrityError:
        # Already exists: use the existing profile.
        db_session.rollback()
        profile = db_session.query(Profile) \
                            .filter(Profile.site=='twitter') \
                            .filter(Profile.original_id==user_id) \
                            .one()

    data = {'name': username, 'site': 'twitter'}

    bio_el = html.select('.ProfileHeaderCard-bio')[0]
    data['description'] = bio_el.get_text()
    profile.description = bio_el.get_text()

    post_count_el = html.select('.ProfileNav-item--tweets .ProfileNav-value')[0]
    data['post_count'] = int(post_count_el.get_text().replace(',', ''))
    profile.post_count = int(post_count_el.get_text().replace(',', ''))

    friend_count_el = html.select('.ProfileNav-item--following .ProfileNav-value')[0]
    data['friend_count'] = int(friend_count_el.get_text().replace(',', ''))
    profile.friend_count = int(friend_count_el.get_text().replace(',', ''))

    follower_count_el = html.select('.ProfileNav-item--followers .ProfileNav-value')[0]
    data['follower_count'] = int(follower_count_el.get_text().replace(',', ''))
    profile.follower_count = int(follower_count_el.get_text().replace(',', ''))

    avatar_el = html.select('.ProfileAvatar-image')[0]
    avatar_url = avatar_el['src']

    db_session.commit()

    app.queue.scrape_queue.enqueue(scrape_twitter_avatar, profile.id, avatar_url)
    app.queue.index_queue.enqueue(worker.index.index_profile, profile.id)

    data['id'] = profile.id

    return data


def scrape_twitter_avatar(id_, url):
    '''
    Get a twitter avatar from ``url`` and save it to the Profile identified by
    ``id_``.
    '''

    redis = worker.get_redis()
    db_session = worker.get_session()

    # Download image. (Twitter doesn't require authentication for static assets.)
    response = requests.get(url, stream=True)

    if response.status_code != 200:
        raise ValueError()

    profile = db_session.query(Profile).filter(Profile.id==id_).first()

    if profile is None:
        raise ValueError('No profile exists with id={}'.format(id_))

    parsed = urllib.parse.urlparse(url)
    name = url.split('/')[-1]

    if 'content-type' in response.headers:
        mime = response.headers['content-type']
    else:
        mime = 'application/octet-stream'

    content = response.raw.read()
    file_ = File(name=name, mime=mime, content=content)
    profile.avatars.append(file_)

    db_session.commit()
    redis.publish('avatar', json.dumps({'id': id_, 'url': '/api/file/' + str(file_.id)}))


def _login_twitter():
    ''' Log into a Twitter account. '''

    redis = worker.get_redis()
    saved_session = redis.get('twitter_session')

    if saved_session is not None:
        try:
            session = pickle.loads(saved_session)
            return session
        except:
            pass

    session = requests.Session()
    twitter_url = 'https://twitter.com'
    home_url = '{}/login'.format(twitter_url)
    home_response = session.get(home_url)

    if home_response.status_code != 200:
        raise ValueError(
            'Not able to fetch Twitter login page ({})'
            .format(home_response.status_code)
        )

    page = bs4.BeautifulSoup(home_response.text, 'html.parser')
    csrf_selector = 'input[name=authenticity_token]'
    csrf_elements = page.select(csrf_selector)

    if len(csrf_elements) == 0:
        raise ValueError(
            'Expected >=1 elements matching selector "{}", found 0 instead.'
            .format(csrf_selector)
        )

    # There may be more than one CSRF element but they should all have the same
    # value, so we arbitrarily take the first one.
    csrf_token = csrf_elements[0]['value']
    login_url = '{}/sessions'.format(twitter_url)

    payload = {
        'authenticity_token': csrf_token,
        'session[username_or_email]': 'testomctester',
        'session[password]': 'Rwtvdw56eegntnO',
        'remember_me': '1',
        'return_to_ssl': 'true',
    }

    login_response = session.post(login_url,
                                  data=payload,
                                  allow_redirects=False)

    if login_response.status_code != 302:
        raise ValueError(
            'Not able to log in to Twitter {}'
            .format(login_response.status_code)
        )
    elif login_response.headers['Location'].startswith(home_url):
        # This indicates that we're being redirected to the login form, e.g.
        # an unsuccessful login attemp.
        raise click.ClickException(
            'Not able to log in to Twitter: probably a bad username or password ({})'
            .format(login_response.status_code)
        )

    saved_session = pickle.dumps(session)
    redis.set('twitter_session', saved_session)

    return session