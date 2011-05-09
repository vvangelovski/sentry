# TODO: this needs to be entirely flask

import base64
try:
    import cPickle as pickle
except ImportError:
    import pickle
import datetime
import logging
import re
import simplejson
import time
import warnings
import zlib

from jinja2 import Markup
from flask import render_template, redirect, request, url_for, \
                  abort, Response

from sentry import app
from sentry.utils import get_filters, is_float, get_signature, parse_auth_header
from sentry.utils.shortcuts import get_object_or_404
from sentry.models import Group, Event
from sentry.plugins import GroupActionProvider
from sentry.web.templatetags import with_priority

uuid_re = re.compile(r'^[a-z0-9]{32}$')

def login_required(func):
    def wrapped(request, *args, **kwargs):
        if not app.config['PUBLIC']:
            if not request.user.is_authenticated():
                return redirect(url_for('login'))
            if not request.user.has_perm('sentry.can_view'):
                return redirect(url_for('login'))
        return func(request, *args, **kwargs)
    wrapped.__doc__ = func.__doc__
    wrapped.__name__ = func.__name__
    return wrapped

@app.route('/auth/login/')
def login(request):
    # TODO:
    pass

@app.route('/auth/logout/')
def logout(request):
    # TODO:
    pass

@login_required
@app.route('/search/')
def search(request):
    try:
        page = int(request.args.get('p', 1))
    except (TypeError, ValueError):
        page = 1

    query = request.args.get('q')
    has_search = bool(app.config['SEARCH_ENGINE'])

    if query:
        if uuid_re.match(query):
            # Forward to message if it exists
            try:
                event = Event.objects.get(query)
            except Event.DoesNotExist:
                pass
            else:
                return redirect(event.get_absolute_url())
        elif not has_search:
            return render_template('sentry/invalid_message_id.html')
        else:
            event_list = get_search_query_set(query)
    else:
        event_list = Group.objects.none()
    
    sort = request.args.get('sort')
    if sort == 'date':
        event_list = event_list.order_by('-last_seen')
    elif sort == 'new':
        event_list = event_list.order_by('-first_seen')
    else:
        sort = 'relevance'

    return render_template('sentry/search.html', {
        'event_list': event_list,
        'query': query,
        'sort': sort,
        'request': request,
    })

@login_required
@app.route('/')
def index():
    filters = []
    for filter_ in get_filters():
        filters.append(filter_(request))

    try:
        page = int(request.args.get('p', 1))
    except (TypeError, ValueError):
        page = 1

    query = request.args.get('content')
    is_search = query

    event_list = Group.objects.all()

    sort = request.args.get('sort')
    if sort == 'date':
        event_list = event_list.order_by('-last_seen')
    elif sort == 'new':
        event_list = event_list.order_by('-first_seen')
    elif sort == 'count':
        event_list = event_list.order_by('-count')
    else:
        sort = 'priority'
        event_list = event_list.order_by('-score')

    filters = []

    any_filter = False
    # for filter_ in filters:
    #     if not filter_.is_set():
    #         continue
    #     any_filter = True
        # event_list = filter_.get_query_set(event_list)

    today = datetime.datetime.now()

    has_realtime = page == 1

    return render_template('sentry/index.html', **{
        'has_realtime': has_realtime,
        'event_list': event_list,
        'today': today,
        'query': query,
        'sort': sort,
        'any_filter': any_filter,
        'request': request,
        'filters': filters,
    })

@login_required
@app.route('/api/')
def ajax_handler():
    op = request.form.get('op')

    if op == 'poll':
        filters = []
        for filter_ in get_filters():
            filters.append(filter_(request))

        event_list = Group.objects

        sort = request.args.get('sort')
        if sort == 'date':
            event_list = event_list.order_by('-last_seen')
        elif sort == 'new':
            event_list = event_list.order_by('-first_seen')
        elif sort == 'count':
            event_list = event_list.order_by('-count')
        else:
            sort = 'priority'
            event_list = event_list.order_by('-score')

        # for filter_ in filters:
        #     if not filter_.is_set():
        #         continue
        #     event_list = filter_.get_query_set(event_list)

        data = [
            (m.pk, {
                'html': render_template('sentry/partial/group.html', **{
                    'group': m,
                    'priority': p,
                    'request': request,
                }),
                'count': m.times_seen,
                'priority': p,
            }) for m, p in with_priority(event_list[0:15])]

    elif op == 'resolve':
        gid = request.REQUEST.get('gid')
        if not gid:
            abort(403)
        try:
            group = Group.objects.get(pk=gid)
        except Group.DoesNotExist:
            abort(403)

        group.update(status=1)

        if not request.is_ajax():
            return redirect(request.environ['HTTP_REFERER'])

        data = [
            (m.pk, {
                'html': render_template('sentry/partial/group.html', **{
                    'group': m,
                    'request': request,
                }),
                'count': m.times_seen,
            }) for m in [group]]
    else:
        abort(400)

    return Response(simplejson.dumps(data), mimetype='application/json')

@login_required
@app.route('/group/<group_id>')
def group_details(group_id):
    group = get_object_or_404(Group, pk=group_id)
    
    last_event = group.get_relations(Event, limit=1)[0]

    def iter_data(obj):
        for k, v in obj.data.iteritems():
            if k.startswith('_') or k in ['url']:
                continue
            yield k, v

    # Render our event's custom output
    processor = last_event.get_processor()
    event_html = Markup(processor.to_html(last_event, last_event.data.get('__event__')))
    
    return render_template('sentry/group/details.html', **{
        'page': 'details',
        'group': group,
        'json_data': iter_data(last_event),
        'event_html': event_html,
    })

@login_required
@app.route('/group/<group_id>/events/')
def group_event_list(group_id):
    group = get_object_or_404(Group, pk=group_id)

    event_list = group.get_relations(Event)

    page = 'events'

    return render_template('sentry/group/event_list.html', **{
        'page': 'events',
        'group': group,
        'event_list': event_list,
    })

@login_required
@app.route('/group/<group_id>/events/<event_id>/')
def group_event_details(group_id, event_id):
    group = get_object_or_404(Group, pk=group_id)
    event = get_object_or_404(Event, pk=event_id)

    def iter_data(obj):
        for k, v in obj.data.iteritems():
            if k.startswith('_') or k in ['url']:
                continue
            yield k, v

    # Render our event's custom output
    processor = event.get_processor()
    event_html = Markup(processor.to_html(event, event.data.get('__event__')))

    return render_template('sentry/group/event.html', **{
        'page': 'events',
        'json_data': iter_data(event),
        'group': group,
        'event': event,
        'event_html': event_html,
    })

@app.route('/store/', methods=['POST'])
def store():
    if request.environ.get('AUTHORIZATION', '').startswith('Sentry'):
        auth_vars = parse_auth_header(request.META['AUTHORIZATION'])
        
        signature = auth_vars.get('sentry_signature')
        timestamp = auth_vars.get('sentry_timestamp')

        format = 'json'

        data = request.raw_post_data

        # Signed data packet
        if signature and timestamp:
            try:
                timestamp = float(timestamp)
            except ValueError:
                abort(400, 'Invalid Timestamp')

            if timestamp < time.time() - 3600: # 1 hour
                abort(410, 'Message has expired')

            sig_hmac = get_signature(data, timestamp)
            if sig_hmac != signature:
                abort(403, 'Invalid signature')
        else:
            abort(401,'Unauthorized')
    else:
        data = request.form.get('data')
        if not data:
            abort(400, 'Missing data')

        format = request.form.get('format', 'pickle')

        if format not in ('pickle', 'json'):
            abort(400, 'Invalid format')

        # Legacy request (deprecated as of 2.0)
        key = request.form.get('key')
        
        if key != app.config['KEY']:
            warnings.warn('A client is sending the `key` parameter, which will be removed in Sentry 2.0', DeprecationWarning)
            abort(403, 'Invalid credentials')

    logger = logging.getLogger('sentry.server')

    try:
        try:
            data = base64.b64decode(data).decode('zlib')
        except zlib.error:
            data = base64.b64decode(data)
    except Exception, e:
        # This error should be caught as it suggests that there's a
        # bug somewhere in the client's code.
        logger.exception('Bad data received')
        abort(400, 'Bad data decoding request (%s, %s)' % (e.__class__.__name__, e))

    try:
        if format == 'pickle':
            data = pickle.loads(data)
        elif format == 'json':
            data = simplejson.loads(data)
    except Exception, e:
        # This error should be caught as it suggests that there's a
        # bug somewhere in the client's code.
        logger.exception('Bad data received')
        abort(403, 'Bad data reconstructing object (%s, %s)' % (e.__class__.__name__, e))

    # XXX: ensure keys are coerced to strings
    data = dict((str(k), v) for k, v in data.iteritems())

    if 'timestamp' in data:
        if is_float(data['timestamp']):
            data['timestamp'] = datetime.datetime.fromtimestamp(float(data['timestamp']))
        else:
            if '.' in data['timestamp']:
                format = '%Y-%m-%dT%H:%M:%S.%f'
            else:
                format = '%Y-%m-%dT%H:%M:%S'
            data['timestamp'] = datetime.datetime.strptime(data['timestamp'], format)

    # TODO
    store()
    
    return ''

@login_required
@app.route('/group/<group_id>/<path:slug>')
def group_plugin_action(group_id, slug):
    group = get_object_or_404(Group, pk=group_id)
    
    try:
        cls = GroupActionProvider.plugins[slug]
    except KeyError:
        abort(404, 'Plugin not found')
    response = cls(group_id)(request, group)
    if response:
        return response
    return redirect(request.environ.get('HTTP_REFERER') or url_for('index'))