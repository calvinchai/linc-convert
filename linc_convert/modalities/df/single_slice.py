"""
Converts JPEG2000 files generated by MBF-Neurolucida into a OME-ZARR pyramid.

It does not recompute the image pyramid but instead reuse the JPEG2000 levels
(obtained by wavelet transform).
"""
# stdlib
import ast
import os

# externals
import glymur
import nibabel as nib
import numpy as np
import zarr
from cyclopts import App

# internals
from linc_convert.modalities.df.cli import df
from linc_convert.utils.j2k import WrappedJ2K, get_pixelsize
from linc_convert.utils.math import ceildiv
from linc_convert.utils.orientation import center_affine, orientation_to_affine
from linc_convert.utils.zarr import make_compressor

ss = App(name="singleslice", help_format="markdown")
df.command(ss)


@ss.default
def convert(
    inp: str,
    out: str | None = None,
    *,
    chunk: int = 1024,
    compressor: str = "blosc",
    compressor_opt: str = "{}",
    max_load: int = 16384,
    nii: bool = False,
    orientation: str = "coronal",
    center: bool = True,
    thickness: float | None = None,
) -> None:
    """
    Convert JPEG2000 files generated by MBF-Neurolucida into a Zarr pyramid.

    It does not recompute the image pyramid but instead reuse the JPEG2000
    levels (obtained by wavelet transform).

    Orientation
    -----------
    The anatomical orientation of the slice is given in terms of RAS axes.

    It is a combination of two letters from the set
    `{"L", "R", "A", "P", "I", "S"}`, where

    * the first letter corresponds to the horizontal dimension and
        indicates the anatomical meaning of the _right_ of the jp2 image,
    * the second letter corresponds to the vertical dimension and
        indicates the anatomical meaning of the _bottom_ of the jp2 image.

    We also provide the aliases

    * `"coronal"` == `"LI"`
    * `"axial"` == `"LP"`
    * `"sagittal"` == `"PI"`

    The orientation flag is only useful when converting to nifti-zarr.

    Parameters
    ----------
    inp
        Path to the input JP2 file
    out
        Path to the output Zarr directory [<INP>.ome.zarr]
    chunk
        Output chunk size
    compressor : {blosc, zlib, raw}
        Compression method
    compressor_opt
        Compression options
    max_load
        Maximum input chunk size
    nii
        Convert to nifti-zarr. True if path ends in ".nii.zarr"
    orientation
        Orientation of the slice
    center
        Set RAS[0, 0, 0] at FOV center
    thickness
        Slice thickness
    """
    if not out:
        out = os.path.splitext(inp)[0]
        out += ".nii.zarr" if nii else ".ome.zarr"

    nii = nii or out.endswith(".nii.zarr")

    if isinstance(compressor_opt, str):
        compressor_opt = ast.literal_eval(compressor_opt)

    j2k = glymur.Jp2k(inp)
    vxw, vxh = get_pixelsize(j2k)

    # Prepare Zarr group
    omz = zarr.storage.DirectoryStore(out)
    omz = zarr.group(store=omz, overwrite=True)

    # Prepare chunking options
    opt = {
        "chunks": list(j2k.shape[2:]) + [chunk, chunk],
        "dimension_separator": r"/",
        "order": "F",
        "dtype": np.dtype(j2k.dtype).str,
        "fill_value": None,
        "compressor": make_compressor(compressor, **compressor_opt),
    }

    # Write each level
    nblevel = j2k.codestream.segment[2].num_res
    has_channel = j2k.ndim - 2
    for level in range(nblevel):
        subdat = WrappedJ2K(j2k, level=level)
        shape = subdat.shape
        print("Convert level", level, "with shape", shape)
        omz.create_dataset(str(level), shape=shape, **opt)
        array = omz[str(level)]
        if max_load is None or (shape[-2] < max_load and shape[-1] < max_load):
            array[...] = subdat[...]
        else:
            ni = ceildiv(shape[-2], max_load)
            nj = ceildiv(shape[-1], max_load)
            for i in range(ni):
                for j in range(nj):
                    print(f"\r{i+1}/{ni}, {j+1}/{nj}", end="")
                    array[
                        ...,
                        i*max_load:min((i+1)*max_load, shape[-2]),
                        j*max_load:min((j+1)*max_load, shape[-1]),
                    ] = subdat[
                        ...,
                        i*max_load:min((i+1)*max_load, shape[-2]),
                        j*max_load:min((j+1)*max_load, shape[-1]),
                    ]
            print("")

    # Write OME-Zarr multiscale metadata
    print("Write metadata")
    multiscales = [{
        "version": "0.4",
        "axes": [
            {"name": "y", "type": "space", "unit": "micrometer"},
            {"name": "x", "type": "space", "unit": "micrometer"}
        ],
        "datasets": [],
        "type": "jpeg2000",
        "name": "",
    }]
    if has_channel:
        multiscales[0]["axes"].insert(0, {"name": "c", "type": "channel"})

    for n in range(nblevel):
        shape0 = omz["0"].shape[-2:]
        shape = omz[str(n)].shape[-2:]
        multiscales[0]["datasets"].append({})
        level = multiscales[0]["datasets"][-1]
        level["path"] = str(n)

        # I assume that wavelet transforms end up aligning voxel edges
        # across levels, so the effective scaling is the shape ratio,
        # and there is a half voxel shift wrt to the "center of first voxel"
        # frame
        level["coordinateTransformations"] = [
            {
                "type": "scale",
                "scale": [1.0] * has_channel + [
                    (shape0[0]/shape[0])*vxh,
                    (shape0[1]/shape[1])*vxw,
                ]
            },
            {
                "type": "translation",
                "translation": [0.0] * has_channel + [
                    (shape0[0]/shape[0] - 1)*vxh*0.5,
                    (shape0[1]/shape[1] - 1)*vxw*0.5,
                ]
            }
        ]
    multiscales[0]["coordinateTransformations"] = [
        {
            "scale": [1.0] * (2 + has_channel),
            "type": "scale"
        }
    ]
    omz.attrs["multiscales"] = multiscales

    if not nii:
        print("done.")
        return

    # Write NIfTI-Zarr header
    # NOTE: we use nifti2 because dimensions typically do not fit in a short
    # TODO: we do not write the json zattrs, but it should be added in
    #       once the nifti-zarr package is released
    shape = list(reversed(omz["0"].shape))
    if has_channel:
        shape = shape[:2] + [1, 1] + shape[2:]
    affine = orientation_to_affine(orientation, vxw, vxh, thickness or 1)
    if center:
        affine = center_affine(affine, shape[:2])
    header = nib.Nifti2Header()
    header.set_data_shape(shape)
    header.set_data_dtype(omz["0"].dtype)
    header.set_qform(affine)
    header.set_sform(affine)
    header.set_xyzt_units(nib.nifti1.unit_codes.code["micron"])
    header.structarr["magic"] = b"n+2\0"
    header = np.frombuffer(header.structarr.tobytes(), dtype="u1")
    opt = {
        "chunks": [len(header)],
        "dimension_separator": r"/",
        "order": "F",
        "dtype": "|u1",
        "fill_value": None,
        "compressor": None,
    }
    omz.create_dataset("nifti", data=header, shape=shape, **opt)
    print("done.")