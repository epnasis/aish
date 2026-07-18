import struct

def parse_exif(filepath):
    print(f"--- Parsing {filepath} ---")
    with open(filepath, 'rb') as f:
        data = f.read(65536) # Read first 64KB
        
    # Search for Exif header
    idx = data.find(b'Exif\x00\x00')
    if idx == -1:
        print("No Exif header found in first 64KB")
        return
    
    # TIFF header starts here
    tiff_start = idx + 6
    tiff_data = data[tiff_start:]
    
    # Byte order
    byte_order = tiff_data[:2]
    if byte_order == b'II':
        endian = '<'
    elif byte_order == b'MM':
        endian = '>'
    else:
        print("Unknown endianness")
        return
        
    magic = struct.unpack(f'{endian}H', tiff_data[2:4])[0]
    if magic != 42:
        print("Not a valid TIFF header")
        return
        
    ifd0_offset = struct.unpack(f'{endian}I', tiff_data[4:8])[0]
    
    def read_ifd(offset):
        if offset >= len(tiff_data):
            return {}
        num_entries = struct.unpack(f'{endian}H', tiff_data[offset:offset+2])[0]
        entries = {}
        curr = offset + 2
        for _ in range(num_entries):
            tag, dtype, count, val_offset = struct.unpack(f'{endian}HHII', tiff_data[curr:curr+12])
            entries[tag] = (dtype, count, val_offset)
            curr += 12
        next_ifd = struct.unpack(f'{endian}I', tiff_data[curr:curr+4])[0]
        return entries, next_ifd

    try:
        ifd0, next_ifd = read_ifd(ifd0_offset)
        print("IFD0 tags found:", list(ifd0.keys()))
        
        # GPS Info tag is 34853 (0x8825)
        if 34853 in ifd0:
            gps_offset = ifd0[34853][2]
            print(f"GPS IFD offset: {gps_offset}")
            gps_ifd, _ = read_ifd(gps_offset)
            print("GPS IFD tags:")
            for tag, (dtype, count, val) in gps_ifd.items():
                # Read tag value based on type
                # 1 = BYTE, 2 = ASCII, 3 = SHORT, 4 = LONG, 5 = RATIONAL
                print(f"  Tag {tag}: dtype={dtype}, count={count}, raw_val={val}")
                if dtype == 2: # ASCII
                    if count <= 4:
                        s = struct.pack(f'{endian}I', val)[:count].decode('ascii', errors='ignore')
                    else:
                        s = tiff_data[val:val+count].decode('ascii', errors='ignore')
                    print(f"    Value (ASCII): {s}")
                elif dtype == 5: # RATIONAL
                    # 8 bytes per rational (numerator, denominator)
                    rational_offset = val
                    vals = []
                    for _ in range(count):
                        num, den = struct.unpack(f'{endian}II', tiff_data[rational_offset:rational_offset+8])
                        vals.append((num, den))
                        rational_offset += 8
                    print(f"    Value (RATIONAL): {vals}")
                elif dtype == 1: # BYTE
                    print(f"    Value (BYTE): {val}")
    except Exception as e:
        print(f"Error parsing: {e}")

parse_exif('/Users/epnasis/.local/state/aish/uploads/IMG_1348.jpeg')
parse_exif('/Users/epnasis/.local/state/aish/uploads/IMG_1344-2.jpeg')
