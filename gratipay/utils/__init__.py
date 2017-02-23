# encoding: utf8
from __future__ import absolute_import, division, print_function, unicode_literals

import random
from base64 import urlsafe_b64encode, urlsafe_b64decode
from datetime import datetime, timedelta

from aspen import Response, json
from aspen.utils import to_rfc822, utcnow
from dependency_injection import resolve_dependencies
from postgres.cursors import SimpleCursorBase

import gratipay


BEGINNING_OF_EPOCH = to_rfc822(datetime(1970, 1, 1)).encode('ascii')

# Difference between current time and credit card expiring date when
# card is considered as expiring
EXPIRING_DELTA = timedelta(days = 30)


def dict_to_querystring(mapping):
    if not mapping:
        return u''

    arguments = []
    for key, values in mapping.iteritems():
        for val in values:
            arguments.append(u'='.join([key, val]))

    return u'?' + u'&'.join(arguments)


def _munge(website, request, url_prefix, fs_prefix):
    if request.path.raw.startswith(fs_prefix):
        to = url_prefix + request.path.raw[len(fs_prefix):]
        if request.qs.raw:
            to += '?' + request.qs.raw
        website.redirect(to)
    elif request.path.raw.startswith(url_prefix):
        request.path.__init__(fs_prefix + request.path.raw[len(url_prefix):])

def help_aspen_find_well_known(website, request):
    _munge(website, request, '/.well-known/', '/_well-known/')
    _munge(website, request, '/assets/.well-known/', '/assets/_well-known/')

def use_tildes_for_participants(website, request):
    return _munge(website, request, '/~', '/~/')


def canonicalize(redirect, path, base, canonical, given, arguments=None):
    if given != canonical:
        assert canonical.lower() == given.lower()  # sanity check
        remainder = path[len(base + given):]

        if arguments is not None:
            arguments = dict_to_querystring(arguments)

        newpath = base + canonical + remainder + arguments or ''
        redirect(newpath)


def get_participant(state, restrict=True, resolve_unclaimed=True):
    """Given a Request, raise Response or return Participant.

    If restrict is True then we'll restrict access to owners and admins.

    """
    redirect = state['website'].redirect
    request = state['request']
    user = state['user']
    slug = request.line.uri.path['username']
    qs = request.line.uri.querystring
    _ = state['_']

    if restrict:
        if user.ANON:
            raise Response(401, _("You need to log in to access this page."))

    from gratipay.models.participant import Participant  # avoid circular import
    participant = Participant.from_username(slug)

    if participant is None:
        raise Response(404)

    canonicalize(redirect, request.line.uri.path.raw, '/~/', participant.username, slug, qs)

    if participant.is_closed:
        if user.ADMIN:
            return participant
        raise Response(410)

    if participant.claimed_time is None and resolve_unclaimed:
        to = participant.resolve_unclaimed()
        if to:
            # This is a stub account (someone on another platform who hasn't
            # actually registered with Gratipay yet)
            redirect(to)
        else:
            # This is an archived account (result of take_over)
            if user.ADMIN:
                return participant
            raise Response(404)

    if restrict:
        if participant != user.participant:
            if not user.ADMIN:
                raise Response(403, _("You are not authorized to access this page."))

    return participant


def get_team(state):
    """Given a Request, raise Response or return Team.
    """
    redirect = state['website'].redirect
    request = state['request']
    user = state['user']
    slug = request.line.uri.path['team']
    qs = request.line.uri.querystring

    from gratipay.models.team import Team  # avoid circular import
    team = Team.from_slug(slug)

    if team is None:
        # Try to redirect to a Participant.
        from gratipay.models.participant import Participant # avoid circular import
        participant = Participant.from_username(slug)
        if participant is not None:
            qs = '?' + request.qs.raw if request.qs.raw else ''
            redirect('/~' + request.path.raw[1:] + qs)
        raise Response(404)

    canonicalize(redirect, request.line.uri.path.raw, '/', team.slug, slug, qs)

    if team.is_closed and not user.ADMIN:
        raise Response(410)

    return team


def encode_for_querystring(s):
    """Given a unicode, return a unicode that's safe for transport across a querystring.
    """
    if not isinstance(s, unicode):
        raise TypeError('unicode required')
    return urlsafe_b64encode(s.encode('utf8')).replace(b'=', b'~').decode('ascii')


def decode_from_querystring(s, **kw):
    """Given a unicode computed by encode_for_querystring, return the inverse.

    We raise Response(400) if the input value can't be decoded (i.e., it's not
    ASCII, not padded properly, or not decodable as UTF-8 once Base64-decoded).

    """
    if not isinstance(s, unicode):
        raise TypeError('unicode required')
    try:
        return urlsafe_b64decode(s.encode('ascii').replace(b'~', b'=')).decode('utf8')
    except:
        if 'default' in kw:
            # Enable callers to handle errors without using try/except.
            return kw['default']
        raise Response(400, "invalid input")


def update_cta(website):
    nusers = website.db.one("""
        SELECT nusers FROM paydays
        ORDER BY ts_end DESC LIMIT 1
    """, default=0)
    nreceiving_from = website.db.one("""
        SELECT nreceiving_from
          FROM teams
         WHERE slug = 'Gratipay'
    """, default=0)
    website.support_current = cur = int(round(nreceiving_from / nusers * 100)) if nusers else 0
    if cur < 10:    goal = 20
    elif cur < 15:  goal = 30
    elif cur < 25:  goal = 40
    elif cur < 35:  goal = 50
    elif cur < 45:  goal = 60
    elif cur < 55:  goal = 70
    elif cur < 65:  goal = 80
    elif cur > 70:  goal = None
    website.support_goal = goal


def _execute(this, sql, params=[]):
    print(sql.strip(), params)
    super(SimpleCursorBase, this).execute(sql, params)

def log_cursor(f):
    "Prints sql and params to stdout. Works globaly so watch for threaded use."
    def wrapper(*a, **kw):
        try:
            SimpleCursorBase.execute = _execute
            ret = f(*a, **kw)
        finally:
            del SimpleCursorBase.execute
        return ret
    return wrapper


def format_money(money):
    format = '%.2f' if money < 1000 else '%.0f'
    return format % money


def truncate(text, target=160, append=' …'):
    nchars = len(text)
    if nchars <= target:                                    # short enough already
        return text
    if append:                                              # recursive case
        return truncate(text, max(target-len(append), 0), '') + append
    truncated = text[:target]
    if not target or ' ' in (truncated[-1], text[target]):  # clean break
        return truncated.rstrip()
    return truncated.rsplit(' ', 1)[0]                      # trailing partial word


def is_card_expiring(expiration_year, expiration_month):
    now = datetime.utcnow()
    expiring_date = datetime(expiration_year, expiration_month, 1)
    delta = expiring_date - now
    return delta < EXPIRING_DELTA


def set_cookie(cookies, key, value, expires=None, httponly=True, path=b'/'):
    cookies[key] = value
    cookie = cookies[key]
    if expires:
        if isinstance(expires, timedelta):
            expires += utcnow()
        if isinstance(expires, datetime):
            expires = to_rfc822(expires).encode('ascii')
        cookie[b'expires'] = expires
    if httponly:
        cookie[b'httponly'] = True
    if path:
        cookie[b'path'] = path
    if gratipay.use_secure_cookies:
        cookie[b'secure'] = True


def erase_cookie(cookies, key, **kw):
    set_cookie(cookies, key, '', BEGINNING_OF_EPOCH, **kw)


def filter_profile_nav(user, participant, pages):
    out = []
    for foo, bar, show_them, show_others in pages:
        if (user.participant == participant and show_them) \
        or (user.participant != participant and show_others) \
        or user.ADMIN:
            out.append((foo, bar, show_them, show_others))
    return out


def to_javascript(obj):
    """For when you want to inject an object into a <script> tag.
    """
    return json.dumps(obj).replace('</', '<\\/')


class LazyResponse(Response):

    def __init__(self, code, lazy_body, **kw):
        Response.__init__(self, code, '', **kw)
        self.lazy_body = lazy_body

    def render_body(self, state):
        f = self.lazy_body
        self.body = f(*resolve_dependencies(f, state).as_args)


def get_featured_projects(popular, unpopular):
    np, nu = len(popular), len(unpopular)

    # surely optimizable & clarifiable, but it passes the tests
    if np < 7 and nu < 3:     p, u = np, nu
    elif np < 7 and nu >= 3:  p, u = np, 10 - np
    elif np >= 7 and nu < 3:  p, u = 10 - nu, nu
    else:                     p, u = 7, 3

    featured_projects = random.sample(popular, p) + random.sample(unpopular, u)
    random.shuffle(featured_projects)
    return featured_projects


def set_version_header(response, website):
    response.headers['X-Gratipay-Version'] = website.version
