#!/usr/bin/env python3
""" Plugin to blend the edges of the face between the swap and the original face. """
import logging
from typing import List, Literal, Optional, Tuple

import cv2
import numpy as np

from lib.align import BlurMask, DetectedFace
from lib.config import FaceswapConfig
from plugins.convert._config import Config

logger = logging.getLogger(__name__)


class Mask():  # pylint:disable=too-few-public-methods
    """ Manipulations to perform to the mask that is to be applied to the output of the Faceswap
    model.

    Parameters
    ----------
    mask_type: str
        The mask type to use for this plugin
    output_size: int
        The size of the output from the Faceswap model.
    coverage_ratio: float
        The coverage ratio that the Faceswap model was trained at.
    configfile: str, Optional
        Optional location of custom configuration ``ini`` file. If ``None`` then use the default
        config location. Default: ``None``
    config: :class:`lib.config.FaceswapConfig`, Optional
        Optional pre-loaded :class:`lib.config.FaceswapConfig`. If passed, then this will be used
        over any configuration on disk. If ``None`` then it is ignored. Default: ``None``

    """
    def __init__(self,
                 mask_type: str,
                 output_size: int,
                 coverage_ratio: float,
                 configfile: Optional[str] = None,
                 config: Optional[FaceswapConfig] = None) -> None:
        logger.debug("Initializing %s: (mask_type: '%s', output_size: %s, coverage_ratio: %s, "
                     "configfile: %s, config: %s)", self.__class__.__name__, mask_type,
                     coverage_ratio, output_size, configfile, config)
        self._mask_type = mask_type
        self._config = self._set_config(configfile, config)
        logger.debug("config: %s", self._config)

        self._coverage_ratio = coverage_ratio
        self._box = self._get_box(output_size)

        erode_types = [f"erosion{f}" for f in ["", "_left", "_top", "_right", "_bottom"]]
        self._erodes = [self._config.get(erode, 0) / 100 for erode in erode_types]
        self._do_erode = any(amount != 0 for amount in self._erodes)

    def _set_config(self,
                    configfile: Optional[str],
                    config: Optional[FaceswapConfig]) -> dict:
        """ Set the correct configuration for the plugin based on whether a config file
        or pre-loaded config has been passed in.

        Parameters
        ----------
        configfile: str
            Location of custom configuration ``ini`` file. If ``None`` then use the
            default config location
        config: :class:`lib.config.FaceswapConfig`
            Pre-loaded :class:`lib.config.FaceswapConfig`. If passed, then this will be
            used over any configuration on disk. If ``None`` then it is ignored.

        Returns
        -------
        dict
            The configuration in dictionary form for the given from
            :attr:`lib.config.FaceswapConfig.config_dict`
        """
        section = ".".join(self.__module__.split(".")[-2:])
        if config is None:
            retval = Config(section, configfile=configfile).config_dict
        else:
            config.section = section
            retval = config.config_dict
            config.section = None
        logger.debug("Config: %s", retval)
        return retval

    def _get_box(self, output_size: int) -> np.ndarray:
        """ Apply a gradient overlay to the edge of the swap box to smooth out any hard areas
        that where the face intersects with the edge of the swap area.

        Gradient is created from 1/16th distance from the edge of the face box and uses the
        parameters as provided for mask blend settings

        Parameters
        ----------
        output_size: int
            The size of the box that contains the swapped face

        Returns
        -------
        :class:`numpy.ndarray`
            The box mask
        """
        box = np.zeros((output_size, output_size, 1), dtype="float32")
        edge = output_size // 32
        box[edge:-edge, edge:-edge] = 1.0

        box = BlurMask(self._config["type"],
                       box, self._config["kernel_size"],
                       self._config["passes"]).blurred
        return box

    def run(self,
            detected_face: DetectedFace,
            sub_crop_offset: Optional[np.ndarray],
            centering: Literal["legacy", "face", "head"],
            predicted_mask: Optional[np.ndarray] = None) -> Tuple[np.ndarray, np.ndarray]:
        """ Obtain the requested mask type and perform any defined mask manipulations.

        Parameters
        ----------
        detected_face: :class:`lib.align.DetectedFace`
            The DetectedFace object as returned from :class:`scripts.convert.Predictor`.
        sub_crop_offset: :class:`numpy.ndarray`, optional
            The (x, y) offset to crop the mask from the center point.
        centering: [`"legacy"`, `"face"`, `"head"`]
            The centering to obtain the mask for
        predicted_mask: :class:`numpy.ndarray`, optional
            The predicted mask as output from the Faceswap Model, if the model was trained
            with a mask, otherwise ``None``. Default: ``None``.

        Returns
        -------
        mask: :class:`numpy.ndarray`
            The mask with all requested manipulations applied
        raw_mask: :class:`numpy.ndarray`
            The mask with no erosion/dilation applied
        """
        logger.trace("Performing mask adjustment: (detected_face: %s, "  # type: ignore
                     "sub_crop_offset: %s, centering: '%s', predicted_mask: %s",
                     detected_face, sub_crop_offset, centering, predicted_mask is not None)
        mask = self._get_mask(detected_face, predicted_mask, centering, sub_crop_offset)
        raw_mask = mask.copy()

        if self._config.get("type") is not None and self._do_erode:
            mask = self._erode(mask)
        logger.trace(  # type: ignore
            "mask shape: %s, raw_mask shape: %s", mask.shape, raw_mask.shape)

        if self._mask_type != "none":
            mask *= self._box

        return mask, raw_mask

    def _get_mask(self,
                  detected_face: DetectedFace,
                  predicted_mask: Optional[np.ndarray],
                  centering: Literal["legacy", "face", "head"],
                  sub_crop_offset: Optional[np.ndarray]) -> np.ndarray:
        """ Return the requested mask with any requested blurring applied.

        Parameters
        ----------
        detected_face: :class:`lib.align.DetectedFace`
            The DetectedFace object as returned from :class:`scripts.convert.Predictor`.
        predicted_mask: :class:`numpy.ndarray`
            The predicted mask as output from the Faceswap Model if the model was trained
            with a mask, otherwise ``None``
        centering: [`"legacy"`, `"face"`, `"head"`]
            The centering to obtain the mask for
        sub_crop_offset: :class:`numpy.ndarray`
            The (x, y) offset to crop the mask from the center point. Set to `None` if the mask
            does not need to be offset for alternative centering

        Returns
        -------
        :class:`numpy.ndarray`
            The requested mask.
        """
        if self._mask_type == "none":
            mask = np.ones_like(self._box)  # Return a dummy mask if not using a mask
        elif self._mask_type == "predicted" and predicted_mask is not None:
            mask = predicted_mask
        else:
            mask = self._get_stored_mask(detected_face, centering, sub_crop_offset)

        logger.trace(mask.shape)  # type: ignore
        return mask

    def _get_stored_mask(self,
                         detected_face: DetectedFace,
                         centering: Literal["legacy", "face", "head"],
                         sub_crop_offset: Optional[np.ndarray]) -> np.ndarray:
        """ get the requested stored mask from the detected face object.

        Parameters
        ----------
        detected_face: :class:`lib.align.DetectedFace`
            The DetectedFace object as returned from :class:`scripts.convert.Predictor`.
        centering: [`"legacy"`, `"face"`, `"head"`]
            The centering to obtain the mask for
        sub_crop_offset: :class:`numpy.ndarray`
            The (x, y) offset to crop the mask from the center point. Set to `None` if the mask
            does not need to be offset for alternative centering

        Returns
        -------
        :class:`numpy.ndarray`
            The mask sized to Faceswap model output with any requested blurring applied.
        """
        mask = detected_face.mask[self._mask_type]
        mask.set_blur_and_threshold(blur_kernel=self._config["kernel_size"],
                                    blur_type=self._config["type"],
                                    blur_passes=self._config["passes"],
                                    threshold=self._config["threshold"])
        if sub_crop_offset is not None and np.any(sub_crop_offset):
            mask.set_sub_crop(sub_crop_offset, centering)
        mask = self._crop_to_coverage(mask.mask)
        mask_size = mask.shape[0]
        face_size = self._box.shape[0]
        if mask_size != face_size:
            interp = cv2.INTER_CUBIC if mask_size < face_size else cv2.INTER_AREA
            mask = cv2.resize(mask,
                              self._box.shape[:2],
                              interpolation=interp)[..., None].astype("float32") / 255.
        return mask

    def _crop_to_coverage(self, mask: np.ndarray) -> np.ndarray:
        """ Crop the mask to the correct dimensions based on coverage ratio.

        Parameters
        ----------
        mask: :class:`numpy.ndarray`
            The original mask to be cropped

        Returns
        -------
        :class:`numpy.ndarray`
            The cropped mask
        """
        if self._coverage_ratio == 1.0:
            return mask
        mask_size = mask.shape[0]
        padding = round((mask_size * (1 - self._coverage_ratio)) / 2)
        mask_slice = slice(padding, mask_size - padding)
        mask = mask[mask_slice, mask_slice, :]
        logger.trace("mask_size: %s, coverage: %s, padding: %s, final shape: %s",  # type: ignore
                     mask_size, self._coverage_ratio, padding, mask.shape)
        return mask

    # MASK MANIPULATIONS
    def _erode(self, mask: np.ndarray) -> np.ndarray:
        """ Erode or dilate mask the mask based on configuration options.

        Parameters
        ----------
        mask: :class:`numpy.ndarray`
            The mask to be eroded or dilated

        Returns
        -------
        :class:`numpy.ndarray`
            The mask with erosion/dilation applied
        """
        kernels = self._get_erosion_kernels(mask)
        if not any(k.any() for k in kernels):
            return mask  # No kernels could be created from selected input res
        eroded = []
        for idx, (kernel, ratio) in enumerate(zip(kernels, self._erodes)):
            if not kernel.any():
                continue
            anchor = [-1, -1]
            if idx > 0:
                pos = 1 if idx % 2 == 0 else 0
                if ratio > 0:
                    val = max(kernel.shape) - 1 if idx < 3 else 0
                else:
                    val = 0 if idx < 3 else max(kernel.shape) - 1
                anchor[pos] = val

            func = cv2.erode if ratio > 0 else cv2.dilate
            eroded.append(func(mask, kernel, iterations=1, anchor=anchor))

        mask = np.min(np.array(eroded), axis=0) if len(eroded) > 1 else eroded[0]
        return mask[..., None]

    def _get_erosion_kernels(self, mask: np.ndarray) -> List[np.ndarray]:
        """ Get the erosion kernels for each of the center, left, top right and bottom erosions.

        An approximation is made based on the number of positive pixels within the mask to create
        an ellipse to act as kernel.

        Parameters
        ----------
        mask: :class:`numpy.ndarray`
            The mask to be eroded or dilated

        Returns
        -------
        list
            The erosion kernels to be used for erosion/dilation
        """
        mask_radius = np.sqrt(np.sum(mask)) / 2
        kernel_sizes = [max(0, int(abs(ratio * mask_radius))) for ratio in self._erodes]
        kernels = []
        for idx, size in enumerate(kernel_sizes):
            kernel = [size, size]
            shape = cv2.MORPH_ELLIPSE if idx == 0 else cv2.MORPH_RECT
            if idx > 1:
                pos = 0 if idx % 2 == 0 else 1
                kernel[pos] = 1  # Set x/y to 1px based on whether eroding top/bottom, left/right
            kernels.append(cv2.getStructuringElement(shape, kernel) if size else np.array(0))
        logger.trace("Erosion kernels: %s", [k.shape for k in kernels])  # type: ignore
        return kernels
