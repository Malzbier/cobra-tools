"""Serialise a loader's blocks and relocations to a single self-contained file.

This is the interchange for formats cobra-tools cannot fully parse. A .motiongraph is the
case that motivated it: 5526 relocations of which 1395 have no Pointer anywhere in the
struct model, so the XML path regenerates 3453 and destroys the rest. BaseFile.raw_export
takes the same data straight from the pool link tables, where nothing is model-dependent,
and this writes that out so a game-specific tool can produce or consume it without
cobra-tools understanding the format at all.

Deliberately NOT a dump of pool bytes alone. An OVL keeps its relocations in the fragment
table rather than in the data, so pointer values inside a block are meaningless and the
table IS most of the information. A blob without it is not reconstructible.

Offsets are block-relative throughout, never pool-absolute, because pools are shared
between loaders and every offset moves when the OVL is rebuilt.

Layout, little-endian:

    magic       4s      b"CBRW"
    version     u32
    mime_ver    u32
    pool_type   u32
    set_pool    u32     NONE when the source had none
    root_block  u32     NONE when the loader has no root
    n_blocks    u32
    n_relocs    u32
    n_deps      u32
    name        zstr    utf-8, NUL-terminated
    ext         zstr
    ovs_name    zstr
    blocks      n_blocks x (pool_type u32, size u32)
    relocs      n_relocs x (src_block u32, src_off u32, dst_block u32, dst_off u32)
                dst_block == NONE marks an empty pointer, whose target is the END of a
                pool; dst_off then names an anchor BLOCK whose pool that is. Recorded as
                an anchor rather than a pool type because resolving by type on import can
                land on a fresh pool that never receives blocks, gets dropped as empty,
                and leaves the relocation pointing at pool index -1.
    deps        n_deps x (src_block u32, src_off u32, name zstr)
    block data  concatenated, in block order, no padding
"""
import struct

# The file suffix packs are written with, and the one create_file routes on. One
# constant deliberately: this becomes the contract external tools are written against, so
# it should be changeable in exactly one place until that contract is settled
PACK_EXT = ".cbrw"
MAGIC = b"CBRW"
VERSION = 1
NONE = 0xFFFFFFFF

_HEAD = struct.Struct("<4s8I")
_BLOCK = struct.Struct("<2I")
_RELOC = struct.Struct("<4I")
_DEP = struct.Struct("<2I")


def _put_str(out, s):
	out.append((s or "").encode("utf-8") + b"\x00")


def _get_str(data, pos):
	end = data.index(b"\x00", pos)
	return data[pos:end].decode("utf-8"), end + 1


def _u32(v):
	"""None and negative both mean 'absent'; everything else must fit unsigned."""
	if v is None or (isinstance(v, int) and v < 0):
		return NONE
	v = int(v)
	if v >= NONE:
		raise ValueError(f"value {v} does not fit a u32 field")
	return v


def pack(exported):
	"""dict from BaseFile.raw_export -> bytes"""
	blocks = exported["blocks"]
	relocs = exported["relocations"]
	deps = exported.get("dependencies", ())
	out = [_HEAD.pack(
		MAGIC, VERSION,
		_u32(exported.get("version")),
		_u32(exported.get("pool_type")),
		_u32(exported.get("set_pool_type")),
		_u32(exported.get("root_block")),
		len(blocks), len(relocs), len(deps))]
	_put_str(out, exported.get("name"))
	_put_str(out, exported.get("ext"))
	_put_str(out, exported.get("ovs_name"))
	for b in blocks:
		out.append(_BLOCK.pack(_u32(b["pool_type"]), len(b["data"])))
	for src_i, src_rel, dst_i, dst_rel in relocs:
		out.append(_RELOC.pack(_u32(src_i), _u32(src_rel), _u32(dst_i), _u32(dst_rel)))
	for src_i, src_rel, name in deps:
		out.append(_DEP.pack(_u32(src_i), _u32(src_rel)))
		_put_str(out, name)
	for b in blocks:
		out.append(b["data"])
	return b"".join(out)


def unpack(data):
	"""bytes -> dict accepted by BaseFile.raw_import"""
	magic, version, mime_ver, pool_type, set_pool, root_block, n_blocks, n_relocs, n_deps \
		= _HEAD.unpack_from(data, 0)
	if magic != MAGIC:
		raise ValueError(f"not a cobra raw pack (magic {magic!r})")
	if version != VERSION:
		# refuse rather than guess: every field below is positional
		raise ValueError(f"unsupported raw pack version {version}, expected {VERSION}")
	pos = _HEAD.size
	name, pos = _get_str(data, pos)
	ext, pos = _get_str(data, pos)
	ovs_name, pos = _get_str(data, pos)

	sizes = []
	for _ in range(n_blocks):
		b_type, b_size = _BLOCK.unpack_from(data, pos)
		pos += _BLOCK.size
		sizes.append((b_type, b_size))

	relocs = []
	for _ in range(n_relocs):
		src_i, src_rel, dst_i, dst_rel = _RELOC.unpack_from(data, pos)
		pos += _RELOC.size
		relocs.append((src_i, src_rel, None if dst_i == NONE else dst_i, dst_rel))

	deps = []
	for _ in range(n_deps):
		src_i, src_rel = _DEP.unpack_from(data, pos)
		pos += _DEP.size
		dep_name, pos = _get_str(data, pos)
		deps.append((src_i, src_rel, dep_name))

	blocks = []
	for b_type, b_size in sizes:
		blocks.append({"pool_type": b_type, "size": b_size,
					   "data": data[pos: pos + b_size]})
		pos += b_size
	if pos != len(data):
		raise ValueError(f"trailing data: consumed {pos} of {len(data)} bytes")

	return {"name": name, "ext": ext, "ovs_name": ovs_name,
			"version": mime_ver,
			"pool_type": None if pool_type == NONE else pool_type,
			"set_pool_type": None if set_pool == NONE else set_pool,
			"root_block": None if root_block == NONE else root_block,
			"blocks": blocks, "relocations": relocs, "dependencies": deps}
