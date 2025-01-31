import torch
import torchvision.transforms as transforms
import torch.utils.data as data
import os
import numpy as np
from PIL import Image

import random
import scipy
import warnings
from pycocotools.coco import COCO
import skimage.color
import skimage.io
import skimage.transform
from distutils.version import LooseVersion

from PIL import Image

classes = [
    "background",
    "PERM",
    "PRIM",
    "filling"
]

class CocoDataset(data.Dataset):
    """COCO Custom Dataset compatible with torch.utils.data.DataLoader."""
    def __init__(self, root, annot_path, step, transform=None, test=False):
        """Set the path for images
        
        Args:
            root: image directory.
            annot_path: coco annotation file path.
            step: indicate the dataset that has been used.
            transform: image transformer.
        """
        if step == 0:
            self.root = os.path.join(root, "PERM")
        elif step == 1:
            self.root = os.path.join(root, "PRIM_class/images")
        elif step == 2:
            self.root = os.path.join(root, "filling")
        else:
            self.root = root
        self.coco = COCO(annot_path)
        self.ids = list(self.coco.imgs.keys())
        self.step = step
        self.transform = transform
        self.test = test

    def __getitem__(self, index):
        """Returns one data pair (image and mask)."""
        img_id = self.ids[index]

        path = self.coco.loadImgs(img_id)[0]['file_name']
        image = Image.open(os.path.join(self.root, path)).convert('RGB')
        image = np.array(image)
        w, h = image.shape[0], image.shape[1]
        image, window, scale, padding, crop = self.resize_image(
                                                image,
                                                min_dim=1024,
                                                min_scale=0,
                                                max_dim=1024)
        image = np.transpose(image, (2, 0, 1))

        # Next we need to create the mask.
        cat_ids = self.coco.getCatIds()
        anns_ids = self.coco.getAnnIds(imgIds=img_id, catIds=cat_ids, iscrowd=None)
        anns = self.coco.loadAnns(anns_ids)
        mask = np.zeros((w, h))
        for ann in anns:
            if self.test:
                mask = np.maximum(mask,self.coco.annToMask(ann)*ann['category_id'])
            else:
                mask = np.maximum(mask,self.coco.annToMask(ann)*(self.step+1))
        mask = self.resize_mask(mask, scale, padding, crop)

        # If augmentation is not None the image and the mask will be augmented
        if self.transform is not None:
            image = self.transform(image)
            mask = self.transform(mask)

        return image, mask

    def __len__(self):
        return len(self.ids)

    def resize_image(self, image, min_dim=None, max_dim=None, min_scale=None, mode="square"):
        """Resizes an image keeping the aspect ratio unchanged.

        min_dim: if provided, resizes the image such that it's smaller
            dimension == min_dim
        max_dim: if provided, ensures that the image longest side doesn't
            exceed this value.
        min_scale: if provided, ensure that the image is scaled up by at least
            this percent even if min_dim doesn't require it.
        mode: Resizing mode.
            none: No resizing. Return the image unchanged.
            square: Resize and pad with zeros to get a square image
                of size [max_dim, max_dim].
            pad64: Pads width and height with zeros to make them multiples of 64.
                  If min_dim or min_scale are provided, it scales the image up
                  before padding. max_dim is ignored in this mode.
                  The multiple of 64 is needed to ensure smooth scaling of feature
                  maps up and down the 6 levels of the FPN pyramid (2**6=64).
            crop: Picks random crops from the image. First, scales the image based
                  on min_dim and min_scale, then picks a random crop of
                  size min_dim x min_dim. Can be used in training only.
                  max_dim is not used in this mode.

        Returns:
        image: the resized image
        window: (y1, x1, y2, x2). If max_dim is provided, padding might
            be inserted in the returned image. If so, this window is the
            coordinates of the image part of the full image (excluding
            the padding). The x2, y2 pixels are not included.
        scale: The scale factor used to resize the image
        padding: Padding added to the image [(top, bottom), (left, right), (0, 0)]
        """
        # Keep track of image dtype and return results in the same dtype
        image_dtype = image.dtype
        # print(image_dtype)
        # Default window (y1, x1, y2, x2) and default scale == 1.
        h, w = image.shape[:2]
        window = (0, 0, h, w)
        scale = 1
        padding = [(0, 0), (0, 0), (0, 0)]
        crop = None

        if mode == "none":
            return image, window, scale, padding, crop

        # Scale?
        if min_dim:
            # Scale up but not down
            scale = max(1, min_dim / min(h, w))
        if min_scale and scale < min_scale:
            scale = min_scale

        # Does it exceed max dim?
        if max_dim and mode == "square":
            image_max = max(h, w)
            if round(image_max * scale) > max_dim:
                scale = max_dim / image_max

        # Resize image using bilinear interpolation
        if scale != 1:
            image = self.resize(image, (round(h * scale), round(w * scale)),
                          preserve_range=True)

        # Need padding or cropping?
        if mode == "square":
            # Get new height and width
            h, w = image.shape[:2]
            top_pad = (max_dim - h) // 2
            bottom_pad = max_dim - h - top_pad
            left_pad = (max_dim - w) // 2
            right_pad = max_dim - w - left_pad
            padding = [(top_pad, bottom_pad), (left_pad, right_pad), (0, 0)]
            image = np.pad(image, padding, mode='constant', constant_values=0)
            window = (top_pad, left_pad, h + top_pad, w + left_pad)
        elif mode == "pad64":
            h, w = image.shape[:2]
            # Both sides must be divisible by 64
            assert min_dim % 64 == 0, "Minimum dimension must be a multiple of 64"
            # Height
            if h % 64 > 0:
                max_h = h - (h % 64) + 64
                top_pad = (max_h - h) // 2
                bottom_pad = max_h - h - top_pad
            else:
                top_pad = bottom_pad = 0
            # Width
            if w % 64 > 0:
                max_w = w - (w % 64) + 64
                left_pad = (max_w - w) // 2
                right_pad = max_w - w - left_pad
            else:
                left_pad = right_pad = 0
            padding = [(top_pad, bottom_pad), (left_pad, right_pad), (0, 0)]
            image = np.pad(image, padding, mode='constant', constant_values=0)
            window = (top_pad, left_pad, h + top_pad, w + left_pad)
        elif mode == "crop":
            # Pick a random crop
            h, w = image.shape[:2]
            y = random.randint(0, (h - min_dim))
            x = random.randint(0, (w - min_dim))
            crop = (y, x, min_dim, min_dim)
            image = image[y:y + min_dim, x:x + min_dim]
            window = (0, 0, min_dim, min_dim)
        else:
            raise Exception("Mode {} not supported".format(mode))
        return image.astype(image_dtype), window, scale, padding, crop


    def resize_mask(self, mask, scale, padding, crop=None):
        """Resizes a mask using the given scale and padding.
        Typically, you get the scale and padding from resize_image() to
        ensure both, the image and the mask, are resized consistently.

        scale: mask scaling factor
        padding: Padding to add to the mask in the form
                [(top, bottom), (left, right), (0, 0)]
        """
        # Suppress warning from scipy 0.13.0, the output shape of zoom() is
        # calculated with round() instead of int()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            mask = scipy.ndimage.zoom(mask, zoom=[scale, scale], order=0)
        if crop is not None:
            y, x, h, w = crop
            mask = mask[y:y + h, x:x + w]
        else:
            padding.pop(2)
            mask = np.pad(mask, padding, mode='constant', constant_values=0)
        return mask

    def resize(self, image, output_shape, order=1, mode='constant', cval=0, clip=True,
               preserve_range=False, anti_aliasing=False, anti_aliasing_sigma=None):
        """A wrapper for Scikit-Image resize().

        Scikit-Image generates warnings on every call to resize() if it doesn't
        receive the right parameters. The right parameters depend on the version
        of skimage. This solves the problem by using different parameters per
        version. And it provides a central place to control resizing defaults.
        """
        if LooseVersion(skimage.__version__) >= LooseVersion("0.14"):
            # New in 0.14: anti_aliasing. Default it to False for backward
            # compatibility with skimage 0.13.
            return skimage.transform.resize(
                image, output_shape,
                order=order, mode=mode, cval=cval, clip=clip,
                preserve_range=preserve_range, anti_aliasing=anti_aliasing,
                anti_aliasing_sigma=anti_aliasing_sigma)
        else:
            return skimage.transform.resize(
                image, output_shape,
                order=order, mode=mode, cval=cval, clip=clip,
                preserve_range=preserve_range)
    