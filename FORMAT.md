# Swatchbin format (Forza Horizon 6, PC)

Here are some notes on the `.swatchbin` texture container as used by Forza Horizon 6 on PC, worked out by comparing headers of files with different sizes, formats and mip counts. `fh6up` rebuilds every original file byte for byte from its own pixel data before writing an upscaled one.

All values are little endian. Offsets are from the start of the file.

## Overall layout

```
[ header (header_size bytes) ][ pixel data (data_size bytes) ]
```

Pixel data is the mip chain in order, largest first, with no padding between levels. Each level is block compressed, so its size is `ceil(w/4) * ceil(h/4) * block_bytes`, with a minimum of one block. Levels smaller than 4x4 still take one full block.

## Header

| Offset | Size | Meaning |
|---|---|---|
| 0 | 4 | Magic `burG` |
| 4 | 4 | Bundle version (`01 01 00 00` = 1.1) |
| 8 | 4 | Header size = offset of pixel data |
| 12 | 4 | Total file size |
| 16 | 4 | Blob count (1) |
| 20 | 4 | Tag `BCXT` |
| 24 | 4 | Blob version |
| 28 | 4 | Offset of metadata chunk (44) |
| 32 | 4 | Header size again (blob data offset) |
| 36 | 4 | Pixel data size |
| 40 | 4 | Pixel data size again (uncompressed size) |
| 44 | 4 | Tag `HCXT` |
| 48 | 2 | `(meta_size << 4) \| flags`, where the low 4 bits are kept from the original |
| 50 | 2 | Unknown, kept |
| 52 | 8 | Internal pointers, kept |
| 60 | 16 | Texture GUID (zero on some UI textures) |
| 76 | 4 | Width |
| 80 | 4 | Height |
| 84 | 4 | Depth (1) |
| 88 | 1 | Array size (1) |
| 89 | 1 | Unknown (0) |
| 90 | 1 | Mip count |
| 91 | 1 | Unknown (4 or 6 observed), kept |
| 92 to 107 | 16 | Zero |
| 108 to 115 | 8 | Internal pointers, kept |
| 116 | 4 | Format code (see below) |
| 120 | 4 | Internal pointer, kept |
| 124 | 4 | `0xFFFFFFFF` |
| 128 | 12 per mip | Mip table |

Pointers inside the metadata are relative to offset 48.

## Sizes

```
meta_size   = 88 + 12 * (mip_count - 1)
header_size = 52 + meta_size
```

Observed: 1 mip = 140 bytes, 9 mips = 236, 10 = 248, 11 = 260.

## Mip table

One 12-byte entry per mip level, starting at offset 128:

| Field | Meaning |
|---|---|
| u32 size | Byte size of this level |
| u32 offset | Offset of this level within the pixel data |
| u32 next | Offset of the next entry relative to 48, i.e. `80 + 12 * (i + 1)`, or `0xFFFFFFFF` on the last entry |

## Format codes

| Code | DXGI format | Bytes per 4x4 block | Typical use |
|---|---|---|---|
| 0 | BC1_UNORM | 8 | Particle diffuse and lighting atlases |
| 3 | BC4_UNORM | 8 | Alpha masks (speaker holes, perforation) |
| 5 | BC5_UNORM | 16 | Normal maps |
| 9 | BC7_UNORM | 16 | Color, roughness/metal/AO, UI art, and some normal maps (character textures) |

Other codes exist in the game (for example 0x13 on a digital gauge capture texture) and are not handled.

## Where textures live

- `media/cars/_library/Textures.zip`: shared car material textures (leather, plastics, fabrics, patterns, speakers). Standard Deflate zip.
- `media/cars/_library/Materials.zip`: `.materialbin` files that reference textures by `Game:\Media\cars\_library\textures\...` paths.
- `media/cars/<CAR>.zip`: per-car models. Interior `.modelbin` files under `Scene/Interior` reference materials and also override textures directly.
- `media/Particles.zip`: particle textures, including the tire smoke flipbooks.

A file placed at the same relative path under `mediapc/` next to `media/` overrides the original.
