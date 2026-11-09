import base64
import io
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from .errors import SyncError

IMAGE_LIMIT = 256 * 1024


class UnusableArtwork(SyncError):
    pass


def select_release_image(images, index="primary"):
    if not images:
        return None
    if index == "primary":
        return next(
            (image for image in images if str(image.get("type", "")).lower() == "primary"),
            images[0],
        )
    try:
        image_index = int(index)
    except (TypeError, ValueError):
        raise SyncError("ARTWORK_IMAGE_INDEX must be 'primary' or an image index")
    if image_index < 0 or image_index >= len(images):
        raise SyncError(
            "ARTWORK_IMAGE_INDEX {} is out of range for {} release images".format(
                image_index, len(images)
            )
        )
    return images[image_index]


def make_spotify_jpeg(image_bytes):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(image_bytes)) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        ValueError,
    ) as error:
        raise UnusableArtwork("Discogs artwork is not a usable image: {}".format(error))
    image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
    while True:
        for quality in (90, 85, 80, 75, 70, 65, 60, 55, 50, 45, 40):
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=quality, optimize=True)
            jpeg = output.getvalue()
            if len(base64.b64encode(jpeg)) <= IMAGE_LIMIT:
                return jpeg
        if image.size == (1, 1):
            break
        image = image.resize(
            (max(1, int(image.width * 0.8)), max(1, int(image.height * 0.8))),
            Image.Resampling.LANCZOS,
        )
    raise SyncError("Discogs artwork could not be compressed under 256 KB")
