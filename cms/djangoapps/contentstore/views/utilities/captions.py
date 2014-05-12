

"""
Views related to operations on course objects
"""
import json
import logging
import os

from django_future.csrf import ensure_csrf_cookie
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseBadRequest, HttpResponseNotFound
from django.utils.translation import ugettext as _

from edxmako.shortcuts import render_to_response
from models.settings.course_grading import CourseGradingModel
from util.json_request import JsonResponse
from xmodule.modulestore.django import modulestore, loc_mapper
from xmodule.modulestore.exceptions import ItemNotFoundError, InvalidLocationError, InsufficientSpecificationError
from xmodule.modulestore.locator import BlockUsageLocator
from xmodule.video_module.transcripts_utils import (
                                    GetTranscriptsFromYouTubeException,
                                    TranscriptsRequestValidationException,
                                    download_youtube_subs)

from ..access import has_course_access
from ..transcripts_ajax import get_transcripts_presence


log = logging.getLogger(__name__)

__all__ = ['utility_captions_handler']


def _get_locator_and_course(package_id, branch, version_guid, block_id, user, depth=0):
    """
    Internal method used to calculate and return the locator and course module
    for the view functions in this file.
    """
    locator = BlockUsageLocator(package_id=package_id, branch=branch, version_guid=version_guid, block_id=block_id)
    if not has_course_access(user, locator):
        raise PermissionDenied()
    course_location = loc_mapper().translate_locator_to_location(locator)
    course_module = modulestore().get_item(course_location, depth=depth)
    return locator, course_module


# pylint: disable=unused-argument
@login_required
def utility_captions_handler(request, tag=None, package_id=None, branch=None, version_guid=None, block=None, utilities_index=None):
    """
    The restful handler for captions requests in the utilities area.
    It provides the list of course videos as well as their status. It also lets
    the user update the captions by pulling the latest version from YouTube.

    GET
        html: return page containing a list of videos in the course
    POST
        json: get the status of the captions of a given video, or update the captions
        of a given video by copying the version of the captions hosted in youtube.
    """
    response_format = request.REQUEST.get('format', 'html')
    if response_format == 'json' or 'application/json' in request.META.get('HTTP_ACCEPT', 'application/json'):
        if request.method == 'POST':
            if request.POST.get('action') == 'update':
                try:
                    locations = _validate_captions_data_update(request)
                except TranscriptsRequestValidationException as e:
                    return error_response(e.message)
                return json_update_videos(request, locations)
            else:
                try:
                    data, item = _validate_captions_data_get(request)
                except TranscriptsRequestValidationException as e:
                    return error_response(e.message)
                return json_get_video_status(data, item)
        else:
            return HttpResponseBadRequest()
    elif request.method == 'GET':  # assume html
        return captions_index(request, package_id, branch, version_guid, block)
    else:
        return HttpResponseNotFound()


@login_required
@ensure_csrf_cookie
def json_update_videos(request, locations):
    """
    Display an editable course overview.

    org, course, name: Attributes of the Location for the item to edit
    """
    results = []
    for key in locations:
        try:
            #update transcripts
            item = modulestore().get_item(key)
            download_youtube_subs({1.0: item.youtube_id_1_0}, item, settings)
            item.sub = item.youtube_id_1_0
            item.save_with_metadata(request.user)

            #get new status
            transcripts_presence = {
                'html5_local': [],
                'html5_equal': False,
                'is_youtube_mode': False,
                'youtube_local': False,
                'youtube_server': False,
                'youtube_diff': True,
                'current_item_subs': None,
                'status': 'Error',
            }
            videos = {'youtube': item.youtube_id_1_0}
            html5 = {}
            for url in item.html5_sources:
                name = os.path.splitext(url.split('/')[-1])[0]
                html5[name] = 'html5'
            videos['html5'] = html5
            captions_dict = get_transcripts_presence(videos, item, transcripts_presence)
            captions_dict.update({'location': key})
            results.append(captions_dict)

        except GetTranscriptsFromYouTubeException as e:
            log.debug(e)
            results.append({'location': key, 'command': e})

    return JsonResponse(results)


@login_required
@ensure_csrf_cookie
def captions_index(request, package_id, branch, version_guid, block):
    """
    Display a list of course videos as well as their status (up to date, or out of date)

    org, course, name: Attributes of the Location for the item to edit
    """
    locator, course = _get_locator_and_course(
        package_id, branch, version_guid, block, request.user, depth=3
    )

    return render_to_response('captions.html',
        {
            'videos': get_videos(course),
            'context_course': course,
            'new_unit_category': 'vertical',
            'course_graders': json.dumps(CourseGradingModel.fetch(locator).graders),
            'locator': locator,
        }
    )


def error_response(message, response=None, status_code=400):
    """
    Simplify similar actions: log message and return JsonResponse with message included in response.

    By default return 400 (Bad Request) Response.
    """
    if response is None:
        response = {}
    log.debug(message)
    response['message'] = message
    return JsonResponse(response, status_code)


def _validate_captions_data_get(request):
    """
    Happens on the 'get' action. Validates, that request contains all proper data for transcripts processing.

    Returns touple of two elements:
        data: dict, loaded json from request,
        item: video item from storage

    Raises `TranscriptsRequestValidationException` if validation is unsuccessful
    or `PermissionDenied` if user has no access.
    """
    try:
        data = json.loads(request.POST.get('video', '{}'))
    except ValueError:
        raise TranscriptsRequestValidationException(_("Invalid location."))

    if not data:
        raise TranscriptsRequestValidationException(_('Incoming video data is empty.'))

    try:
        location = data.get('location')
        item = modulestore().get_item(location)
    except (ItemNotFoundError, InvalidLocationError, InsufficientSpecificationError):
        raise TranscriptsRequestValidationException(_("Can't find item by locator."))

    if item.category != 'video':
        raise TranscriptsRequestValidationException(_('Transcripts are supported only for "video" modules.'))

    return data, item


def _validate_captions_data_update(request):
    """
    Happens on the 'update' action. Validates, that request contains all proper data for transcripts processing.

    Returns data: dict, loaded json from request

    Raises `TranscriptsRequestValidationException` if validation is unsuccessful
    or `PermissionDenied` if user has no access.
    """
    try:
        data = json.loads(request.POST.get('update_array', '[]'))
    except ValueError:
        raise TranscriptsRequestValidationException(_("Invalid locations."))

    if not data:
        raise TranscriptsRequestValidationException(_('Incoming update_array data is empty.'))

    for location in data:
        try:
            item = modulestore().get_item(location)
        except (ItemNotFoundError, InvalidLocationError, InsufficientSpecificationError):
            raise TranscriptsRequestValidationException(_("Can't find item by locator."))

        if item.category != 'video':
            raise TranscriptsRequestValidationException(_('Transcripts are supported only for "video" modules.'))

    return data


def json_get_video_status(video_meta, item):
    """
    Fetches the status of a given video

    Returns: status True if the captions are up to date, and False if the captions are out of date
    """
    transcripts_presence = {
        'html5_local': [],
        'html5_equal': False,
        'is_youtube_mode': False,
        'youtube_local': False,
        'youtube_server': False,
        'youtube_diff': True,
        'current_item_subs': None,
        'status': 'Error',
    }

    videos = {'youtube': item.youtube_id_1_0}
    html5 = {}
    for url in item.html5_sources:
        name = os.path.splitext(url.split('/')[-1])[0]
        html5[name] = 'html5'
    videos['html5'] = html5
    transcripts_presence = get_transcripts_presence(videos, item, transcripts_presence)
    # video_meta['status'] = transcripts_presence['status'] == 'Success' and (transcripts_presence['command'] == 'found' or transcripts_presence['command'] == 'use_existing')
    video_meta.update(transcripts_presence)
    return JsonResponse(video_meta)


def get_videos(course):
    """
    Fetches the list of course videos

    Returns: A list of tuples representing (name, location) of each video
    """
    video_list = []
    for section in course.get_children():
        for subsection in section.get_children():
            for unit in subsection.get_children():
                for component in unit.get_children():
                    if component.location.category == 'video':
                        video_list.append({'name': component.display_name_with_default, 'location': str(component.location)})
    return video_list
