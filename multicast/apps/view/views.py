import time

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import HttpResponse, JsonResponse
from django.http.response import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.http import FileResponse
from wsgiref.util import FileWrapper
from django.shortcuts import get_object_or_404, render
from django.views.decorators.cache import never_cache
from django.db.models import F
from .models import Stream, Tunnel
from .tasks import open_tunnel, start_ffmpeg
import time
import logging

from .forms import DescriptionForm, CustomUserCreationForm
from .models import Category, Description, Stream, TrendingStream, Tunnel

from ...settings import TRENDING_STREAM_MAX_VISIBLE_SIZE
from .tasks import open_tunnel, start_ffmpeg

import os

logger = logging.getLogger(__name__)


def is_ajax(request):
    """
    Calling request.is_ajax() results in the following error:
        AttributeError: 'WSGIRequest' object has no attribute 'is_ajax'
    This function reproduces the functionality of request.is_ajax() without the error.

    References:
        AttributeError: 'WSGIRequest' object has no attribute 'is_ajax',
        https://stackoverflow.com/questions/70419441/attributeerror-wsgirequest-object-has-no-attribute-is-ajax

    :param request:
    :return:
    """
    return request.META.get("HTTP_X_REQUESTED_WITH") == "XMLHttpRequest"


# Allow a user to create an account
def register(request):
    if request.method == "POST":
        form = CustomUserCreationForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect(reverse("login"))
    else:
        form = CustomUserCreationForm()

    return render(request, "registration/register.html", context={"form": form})


# Home page listing all active streamseported_index
def index(request):
    # Get all categories from the database
    categories = Category.objects.all()
    # Get the active category from the request, defaulting to empty string if there is no category
    str_active_category = request.GET.get("category", "")
    # Get all active streams from the database
    stream_list = Stream.objects.filter(active=True).order_by("-created_at")

    if str_active_category:
        # Get a query set with the active category from the database
        active_category_set = categories.filter(slug=str_active_category)
        # Get the distinct streams with categories in the active category set
        stream_list = stream_list.filter(categories__in=active_category_set).distinct()

    # Get the search query from the request
    str_query = request.GET.get("query", "")
    if str_query:
        stream_list = stream_list.filter(description__icontains=str_query)

    # Show 24 streams per page
    paginator = Paginator(stream_list, 24)
    # Get the requested page number
    page_number = request.GET.get("page")
    # Get the page from the request
    page_obj = paginator.get_page(page_number)

    context = {
        "categories": categories,
        "active_category": str_active_category,
        "page_obj": page_obj,
        "query": str_query,
        "TRENDING_STREAM_MAX_VISIBLE_SIZE": TRENDING_STREAM_MAX_VISIBLE_SIZE,
    }
    return render(request, "view/index.html", context)


def trending_index(request):
    stream_list = []
    for trending_stream in TrendingStream.objects.all():
        if trending_stream.stream.active:
            stream_list.append(trending_stream.stream)
    context = {
        "stream_list": stream_list[0:TRENDING_STREAM_MAX_VISIBLE_SIZE],
        "TRENDING_STREAM_MAX_VISIBLE_SIZE": TRENDING_STREAM_MAX_VISIBLE_SIZE,
    }
    return render(request, "view/trending_index.html", context=context)


def editors_choice_index(request):
    context = {
        "stream_list": Stream.objects.filter(editors_choice=True, active=True),
        "TRENDING_STREAM_MAX_VISIBLE_SIZE": TRENDING_STREAM_MAX_VISIBLE_SIZE,
    }
    return render(request, "view/editors_choice_index.html", context=context)


@login_required()
def liked_index(request):
    if request.user.is_authenticated:
        # Get all active and liked streams
        # Reversed in order to show the most recently liked streams first
        liked_streams = reversed(request.user.liked_streams.filter(active=True))
        context = {
            "stream_list": liked_streams,
            "TRENDING_STREAM_MAX_VISIBLE_SIZE": TRENDING_STREAM_MAX_VISIBLE_SIZE,
        }
        return render(request, "view/liked_index.html", context=context)
    raise PermissionDenied


# Detail page for a specific stream
def detail(request, stream_id):
    stream = get_object_or_404(Stream, id=stream_id)

    context = {
        "stream": stream,
        "descriptions": Description.objects.filter(stream=stream).order_by("-votes")[
            :3
        ],
        "description_form": (
            DescriptionForm() if request.user.is_authenticated else None
        ),
        "num_likes": stream.likes.count(),
        "stream_is_liked_by_user": request.user.is_authenticated
        and request.user in stream.likes.all(),
        "TRENDING_STREAM_MAX_VISIBLE_SIZE": TRENDING_STREAM_MAX_VISIBLE_SIZE,
    }

    return render(request, "view/detail.html", context=context)


def serve_media_file(request, path):
    file_path = os.path.join("multicast/media/tunnel-files", path)
    print(f"Requested file path: {file_path}")  # Debugging statement
    try:
        if os.path.exists(file_path):
            print("File exists.")  # Debugging statement
            file_handle = open(file_path, "rb")
            response = HttpResponse(
                FileWrapper(file_handle), content_type="application/octet-stream"
            )
            response["Content-Disposition"] = (
                f'inline; filename="{os.path.basename(file_path)}"'
            )
            response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response["Pragma"] = "no-cache"
            response["Expires"] = "Thu, 01 Jan 1970 00:00:00 GMT"
            return response
        else:
            print("File does not exist at the given path.")  # Debugging statement
            raise FileNotFoundError
    except FileNotFoundError:
        print("FileNotFoundError raised.")
        raise Http404("File not found")


@never_cache
def watch(request, stream_id):
    stream = get_object_or_404(Stream, id=stream_id)
    tunnel, created = Tunnel.objects.get_or_create(stream=stream)

    if not tunnel.amt_gateway_up:
        logger.info(f"Opening AMT tunnel for stream {stream_id}")
        task = open_tunnel.delay(tunnel.id)
        tunnel.amt_gateway_up = True
        tunnel.save()
        
        # Wait for the tunnel to be ready
        max_wait_time = 30  # Maximum wait time in seconds
        start_time = time.time()
        while not task.ready():
            if time.time() - start_time > max_wait_time:
                logger.error(f"Timeout waiting for AMT tunnel to open for stream {stream_id}")
                return HttpResponse("Stream not ready. Please try again later.", status=503)
            time.sleep(0.5)  # Check every 0.5 seconds
        
        if task.failed():
            logger.error(f"Failed to open AMT tunnel for stream {stream_id}")
            return HttpResponse("Failed to prepare stream. Please try again later.", status=503)

    if not tunnel.ffmpeg_up:
        logger.info(f"Starting FFmpeg for stream {stream_id}")
        task = start_ffmpeg.delay(tunnel.id)
        
        # Wait for FFmpeg to start
        max_wait_time = 30  # Maximum wait time in seconds
        start_time = time.time()
        while not task.ready():
            if time.time() - start_time > max_wait_time:
                logger.error(f"Timeout waiting for FFmpeg to start for stream {stream_id}")
                return HttpResponse("Stream not ready. Please try again later.", status=503)
            time.sleep(0.5)  # Check every 0.5 seconds
        
        if task.failed():
            logger.error(f"Failed to start FFmpeg for stream {stream_id}")
            return HttpResponse("Failed to prepare stream. Please try again later.", status=503)

    Tunnel.objects.filter(id=tunnel.id).update(
        active_viewer_count=F("active_viewer_count") + 1
    )

    context = {
        "watch_file": f"/media/tunnel-files/{tunnel.get_filename()}",
    }

    response = render(request, "view/watch.html", context=context)

    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    response["Expires"] = "Thu, 01 Jan 1970 00:00:00 GMT"

    return response


# Download a .m3u file for the user to open in VLC
def open_file(request, stream_id):
    stream = get_object_or_404(Stream, id=stream_id)

    response = HttpResponse()
    response["Content-Disposition"] = 'attachment; filename="playlist.m3u"'
    response.write("amt://{}@{}".format(stream.source, stream.group))
    if stream.udp_port:
        response.write(":{}".format(stream.udp_port))

    return response


# Allow users to report broken streams
def report(request, stream_id):
    stream = get_object_or_404(Stream, id=stream_id)

    if is_ajax(request):
        stream.report()
        return JsonResponse(dict())
    else:
        raise Http404


# Allow users to upvote a description
def upvote_description(request, description_id):
    description = get_object_or_404(Description, id=description_id)

    if is_ajax(request):
        description.upvote()
        return JsonResponse(dict())


# Allow users to downvote a description
def downvote_description(request, description_id):
    description = get_object_or_404(Description, id=description_id)

    if is_ajax(request):
        description.downvote()
        return JsonResponse(dict())


# Allow authenticated user to submit a stream description
@login_required
def submit_description(request, stream_id):
    stream = get_object_or_404(Stream, id=stream_id)

    if request.method == "POST":
        form = DescriptionForm(request.POST)
        if form.is_valid():
            description, created = Description.objects.get_or_create(
                stream=stream,
                text=form.cleaned_data["text"],
                defaults={
                    "user_submitted": request.user,
                },
            )
            if not created:
                description.upvote()
        return redirect(reverse("view:detail", kwargs={"stream_id": stream.id}))
    raise Http404


# Allow admins to review broken streams
@login_required
def broken_index(request):
    if request.user.is_superuser:
        context = {"stream_list": Stream.objects.filter(active=False)}
        return render(request, "view/broken_index.html", context=context)
    raise PermissionDenied


# Allow admins to take action on broken streams
@login_required
def broken_detail(request, stream_id):
    if request.user.is_superuser:
        stream = get_object_or_404(Stream, id=stream_id)

        if request.method == "POST":
            stream.delete()
            return redirect(reverse("view:broken_index"))
        else:
            context = {
                "stream": stream,
                "descriptions": Description.objects.filter(stream=stream).order_by(
                    "-votes"
                )[:3],
            }

            return render(request, "view/broken_detail.html", context=context)
    raise PermissionDenied


# Clear the reports and/or inactivity associated with a stream
@login_required
def broken_clear(request, stream_id):
    if request.user.is_superuser:
        stream = get_object_or_404(Stream, id=stream_id)
        if is_ajax(request):
            stream.report_count = 0
            stream.active = True
            stream.update_last_found()
            stream.save()
            return JsonResponse(dict())
        return Http404
    raise PermissionDenied


@login_required()
def set_editors_choice(request, stream_id):
    if request.user.is_superuser:
        stream = get_object_or_404(Stream, id=stream_id)
        if is_ajax(request):
            value = request.GET.get("editors_choice", "false")
            if value == "true":
                stream.editors_choice = True
                stream.save()
            else:
                stream.editors_choice = False
                stream.save()
            return JsonResponse(dict())
        return Http404
    raise PermissionDenied


@login_required()
def like_stream(request, stream_id):
    if request.user.is_authenticated:
        stream = get_object_or_404(Stream, id=stream_id)
        if is_ajax(request):
            # Check if the user has liked the stream once and then removed his like.
            if request.user in stream.removed_likes.all():
                # Remove the relationship, because the user is now liking the stream again.
                stream.removed_likes.remove(request.user)
            else:
                # This is the first time the user is liking this stream -> Increase the trending score of the stream.
                TrendingStream.objects.add(stream)
            # Add the user to the likes set of the stream
            if not request.user in stream.likes.all():
                stream.likes.add(request.user)
            return JsonResponse(dict())
        return Http404
    raise PermissionDenied


@login_required()
def remove_like_from_stream(request, stream_id):
    if request.user.is_authenticated:
        stream = get_object_or_404(Stream, id=stream_id)
        if is_ajax(request):
            if request.user in stream.likes.all():
                # Remove the user from the likes set of the stream.
                stream.likes.remove(request.user)
                # Add the user to the removed_likes set of the stream.
                # That way we can check later if the user likes the same stream again.
                # Such like will not further increase the trending score of the stream.
                stream.removed_likes.add(request.user)
            return JsonResponse(dict())
        return Http404
    raise PermissionDenied
