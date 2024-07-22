from celery import shared_task
import os
import subprocess
import tempfile

from django.core.files import File
from django.shortcuts import get_object_or_404

from ...settings import MEDIA_ROOT, BASE_DIR
from .amt.constants import LOCAL_LOOPBACK
from .models import Stream, Tunnel
from .util.stream_preview import snapshot_multicast_stream, resize_image
import logging
import time
import glob

logger = logging.getLogger(__name__)


@shared_task
def create_preview_for_stream(stream_id):
    """
    Shared task that creates a thumbnail and a preview for a stream by a given stream ID.

    The task calls a script that connects to the stream and creates a couple of snapshots
    in a temporary directory. If the script was able to create any snapshots, one of them
    is chosen (currently the first one) and from it a thumbnail and a preview are created
    and then saved to the thumbnail and preview fields of the stream.

    :param stream_id:
    :return:
    """
    if stream_id is None:
        ValueError("Illegal argument: stream_id is null!")
    if not isinstance(stream_id, int):
        ValueError("Illegal argument: stream_id is not an integer!")

    # Get the stream object
    stream = Stream.objects.get(id=stream_id)
    # Create a temp directory
    temp_dir = tempfile.TemporaryDirectory()
    # Snapshot the stream and save the images in the temp directory
    amt_relay = (
        stream.amt_relay if stream.amt_relay is not None else "amt-relay.m2icast.net"
    )
    snapshot_multicast_stream(stream.get_url(), amt_relay, temp_dir.name)
    # List the snapshots
    snapshots = os.listdir(temp_dir.name)
    # Check if there are any snapshots
    if snapshots:
        # Get one of the snapshots
        first_snapshot = snapshots[0]
        # Build the path to the snapshot
        str_snapshot_path = os.path.join(temp_dir.name, first_snapshot)

        # Create a temp file for the thumbnail
        with tempfile.NamedTemporaryFile() as thumbnail:
            # Resize the original snapshot and save it to the temp file
            resize_image(str_snapshot_path, thumbnail.name, i_width=440)
            # Get the stream again, so that we don't overwrite some data,
            # which might have changed while taking the snapshots
            stream = Stream.objects.get(id=stream_id)
            # Delete the old file without saving, because the field will be saved on the next line
            stream.thumbnail.delete(save=False)
            # Update the thumbnail in the stream object
            stream.thumbnail.save(
                "stream_" + str(stream_id) + "_thb.jpg", File(thumbnail), save=True
            )

        # Create a temp file for the preview
        with tempfile.NamedTemporaryFile() as preview:
            # Resize the original snapshot and save it to the temp file
            resize_image(str_snapshot_path, preview.name, i_width=880)
            # Get the stream again, so that we don't overwrite some data,
            # which might have changed while taking the snapshots
            stream = Stream.objects.get(id=stream_id)
            # Delete the old file without saving, because the field will be saved on the next line
            stream.preview.delete(save=False)
            # Update the preview in the stream object
            stream.preview.save(
                "stream_" + str(stream_id) + "_prw.jpg", File(preview), save=True
            )

    # Remove the temp directory
    temp_dir.cleanup()


@shared_task
def open_tunnel(tunnel_id):
    tunnel = get_object_or_404(Tunnel, id=tunnel_id)

    relay = tunnel.stream.amt_relay or "amt-relay.m2icast.net"
    source = tunnel.stream.source
    multicast = tunnel.stream.group
    amt_port = str(tunnel.get_amt_port_number())
    udp_port = str(tunnel.get_udp_port_number())

    # Create logs directory if it doesn't exist
    logs_dir = os.path.join(BASE_DIR, "logs", "tunnels")
    os.makedirs(logs_dir, exist_ok=True)

    # Create a log file name based on the tunnel ID and timestamp
    log_file_name = f"tunnel_{tunnel_id}_{int(time.time())}.log"
    log_file_path = os.path.join(logs_dir, log_file_name)

    # Path to the tunnel.py script
    script_path = os.path.join(BASE_DIR, "apps", "view", "amt", "tunnel.py")

    # Construct the command as a list of arguments
    command = [
        "pipenv",
        "run",
        "python3",
        script_path,
        relay,
        source,
        multicast,
        amt_port,
        udp_port,
    ]

    # Open the log file in append mode
    with open(log_file_path, "a") as log_file:
        try:
            # Run the command and redirect stdout and stderr to the log file
            proc = subprocess.Popen(
                command,
                stdout=log_file,
                stderr=subprocess.STDOUT,  # Redirect stderr to stdout
            )

            tunnel.amt_gateway_pid = proc.pid
            tunnel.log_file_path = log_file_path
            tunnel.amt_gateway_up = True
            tunnel.save()

            return f"Tunnel opened for {tunnel_id} with PID {proc.pid}"
        except subprocess.SubprocessError as e:
            error_msg = f"Failed to open tunnel for {tunnel_id}: {str(e)}"
            log_file.write(error_msg + "\n")
            tunnel.amt_gateway_up = False
            tunnel.save()
            raise Exception(error_msg)


@shared_task
def start_ffmpeg(tunnel_id):
    tunnel = get_object_or_404(Tunnel, id=tunnel_id)
    udp_port = tunnel.get_udp_port_number()

    log_dir = os.path.join(MEDIA_ROOT, "tunnel-files", "logs")
    os.makedirs(log_dir, exist_ok=True)

    ffprobe_log_file = os.path.join(log_dir, f"ffprobe_{tunnel_id}.log")
    ffmpeg_log_file = os.path.join(log_dir, f"ffmpeg_{tunnel_id}.log")

    # Run ffprobe (unchanged)
    ffprobe_command = [
        "ffprobe",
        f"udp://127.0.0.1:{udp_port}",
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,codec_name,width,height,bit_rate,sample_rate",
        "-of",
        "json",
    ]
    try:
        with open(ffprobe_log_file, "w") as log:
            subprocess.run(
                ffprobe_command,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=120,
            )
    except subprocess.TimeoutExpired:
        logger.error(f"FFprobe timed out for tunnel {tunnel_id}")
        return f"FFprobe timed out for tunnel {tunnel_id}"
    except subprocess.CalledProcessError as e:
        logger.error(f"FFprobe failed for tunnel {tunnel_id}: {str(e)}")
        return f"FFprobe failed for tunnel {tunnel_id}: {str(e)}"

    # Improved FFmpeg command
    output_file = os.path.join(MEDIA_ROOT, "tunnel-files", tunnel.get_filename())
    ffmpeg_command = [
        "ffmpeg",
        "-i",
        f"udp://127.0.0.1:{udp_port}",
        "-reconnect",
        "1",
        "-reconnect_at_eof",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "2",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-f",
        "hls",
        "-hls_time",
        "2",
        "-hls_list_size",
        "4",  # Keep only 4 segments in the playlist
        "-hls_flags",
        "delete_segments+append_list+discont_start",
        "-hls_segment_type",
        "mpegts",
        "-hls_segment_filename",
        f"{output_file}_%03d.ts",
        "-hls_playlist_type",
        "event",
        "-metadata",
        f"service_name=Stream {tunnel_id}",
        "-metadata",
        f"service_provider=Your Service Name",
        output_file,
    ]

    def cleanup_ts_files():
        ts_files = sorted(glob.glob(f"{output_file}_*.ts"))
        for old_file in ts_files[:-4]:  # Keep only the 4 most recent files
            try:
                os.remove(old_file)
                logger.info(f"Deleted old TS file: {old_file}")
            except OSError as e:
                logger.error(f"Error deleting file {old_file}: {e}")

    try:
        with open(ffmpeg_log_file, "w") as log:
            proc = subprocess.Popen(
                ffmpeg_command, stdout=log, stderr=subprocess.STDOUT
            )

        # Wait for a short time to check if FFmpeg starts successfully
        time.sleep(5)
        if proc.poll() is not None:
            raise subprocess.CalledProcessError(proc.returncode, ffmpeg_command)

        tunnel.ffmpeg_pid = proc.pid
        tunnel.ffmpeg_up = True
        tunnel.save()

        # Start a background task to clean up old TS files periodically
        cleanup_interval = 10  # seconds
        while proc.poll() is None:  # While FFmpeg is still running
            cleanup_ts_files()
            time.sleep(cleanup_interval)

        return f"FFmpeg started for tunnel {tunnel_id} with PID {proc.pid}"
    except Exception as e:
        logger.error(f"Failed to start FFmpeg for tunnel {tunnel_id}: {str(e)}")
        return f"Failed to start FFmpeg for tunnel {tunnel_id}: {str(e)}"
    finally:
        # Ensure one last cleanup is performed when the task ends
        cleanup_ts_files()
