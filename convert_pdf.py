import os
import re
import json
import io
import glob
from pypdf import PdfReader
from PIL import Image

# Directories
os.makedirs("images", exist_ok=True)
os.makedirs("out_json", exist_ok=True)

def find_key_with_spaces(text, key_name):
    pattern_parts = [re.escape(char) for char in key_name]
    pattern = r'\s*' + r'\s*'.join(pattern_parts)
    m = re.search(pattern, text, re.IGNORECASE)
    if m:
        return m.start(), m.end(), m.group(0)
    return -1, -1, ""

def clean_spaced_text(text):
    if not text:
        return text
    # Clean up null bytes
    text = text.replace('\u0000', '')
    
    # Detect if space-padded (e.g. "f o r m e   u n i q u e")
    # We split by spaces and check the proportion of single characters
    tokens = text.split(' ')
    single_char_tokens = [t for t in tokens if len(t) == 1 and t.isalnum()]
    alnum_tokens = [t for t in tokens if t and any(c.isalnum() for c in t)]
    
    if len(alnum_tokens) > 6 and len(single_char_tokens) / len(alnum_tokens) > 0.5:
        # Replace 2 or more spaces with a unique pipeline placeholder
        temp = re.sub(r'\s{2,}', ' ||| ', text)
        # Remove single spaces that are not surrounding the placeholder
        temp = "".join([char for idx, char in enumerate(temp) if char != ' ' or 
                        (idx > 1 and temp[idx-2:idx+1] == '|||') or 
                        (idx < len(temp)-2 and temp[idx:idx+3] == '|||')])
        # Replace placeholder back with a single space
        temp = temp.replace('|||', ' ')
        temp = temp.replace('\u0000', '')
        # Clean up any remaining artifacts
        temp = re.sub(r'\s+', ' ', temp)
        return temp.strip()
    return text

def parse_keys_flexible(text, keys):
    found_keys = []
    for k in keys:
        start_idx, end_idx, matched_str = find_key_with_spaces(text, k)
        if start_idx != -1:
            found_keys.append((start_idx, end_idx, k))
    found_keys.sort()
    
    result = {}
    for i in range(len(found_keys)):
        start_idx, end_idx, key_name = found_keys[i]
        val_start = end_idx
        val_end = found_keys[i+1][0] if i + 1 < len(found_keys) else len(text)
        
        field_name = key_name.rstrip(':').strip().lower().replace(' ', '_')
        val_text = text[val_start:val_end].strip()
        result[field_name] = clean_spaced_text(val_text)
    return result

def get_section(txt, section_name, next_sections):
    start_idx = txt.find(section_name)
    if start_idx == -1:
        # try flexible search if needed, but section headers [CSI DATA] are usually standard
        # let's try a regex search for the section header
        pattern = r'\[\s*' + r'\s*'.join(section_name.strip('[]')) + r'\s*\]'
        m = re.search(pattern, txt, re.IGNORECASE)
        if m:
            start_idx = m.start()
        else:
            return ""
            
    # start after the section header
    header_end = start_idx + len(section_name)
    # search next sections
    end_idx = len(txt)
    for ns in next_sections:
        # build flexible regex for next section headers
        ns_pattern = r'\[\s*' + r'\s*'.join(ns.strip('[]')) + r'\s*\]'
        ns_match = re.search(ns_pattern, txt[header_end:], re.IGNORECASE)
        if ns_match:
            ns_idx = header_end + ns_match.start()
            if ns_idx < end_idx:
                end_idx = ns_idx
    return txt[header_end:end_idx].strip()

def convert_pdf_file(pdf_path, json_output_path):
    print(f"Reading PDF: {pdf_path}")
    reader = PdfReader(pdf_path)

    # 1. Extract all text
    full_text = ""
    for page in reader.pages:
        full_text += page.extract_text() + "\n"

    # 2. Extract all unique large images in order of appearance
    unique_images_data = []
    seen_data = set()

    for page in reader.pages:
        for img_file in page.images:
            data = img_file.data
            if data not in seen_data:
                seen_data.add(data)
                try:
                    img = Image.open(io.BytesIO(data))
                    if img.size[0] > 50 and img.size[1] > 50:
                        unique_images_data.append(data)
                except Exception as e:
                    pass

    # 3. Parse ASIN records
    # Header pattern ASIN: B0DL5H68TB  |  Verdict: FP
    # Let's make it flexible for spacing as well
    pattern = re.compile(r'A\s*S\s*I\s*N\s*:\s*([A-Z0-9]{10})\s*\|\s*V\s*e\s*r\s*d\s*i\s*c\s*t\s*:\s*(TP|FP)', re.IGNORECASE)
    matches = list(pattern.finditer(full_text))
    print(f"Found {len(matches)} ASIN declarations in text. Images extracted: {len(unique_images_data)}")

    records = []
    for i in range(len(matches)):
        start = matches[i].start()
        end = matches[i+1].start() if i + 1 < len(matches) else len(full_text)
        
        asin = matches[i].group(1)
        verdict = matches[i].group(2)
        record_text = full_text[start:end]
        
        records.append({
            'asin': asin,
            'verdict': verdict,
            'text': record_text
        })

    csi_keys = ['item_name:', 'bullet_point:', 'product_description:', 'size:', 'target_gender:', 'age_range_description:', 'material:', 'status:']
    dp_keys = ['Title:', 'Brand Name:', 'Included Components:', 'Country Of Origin:', 'Unit Count:', 'Item Type Name:', 'Model Name:', 'Model Number:', 'Manufacturer Part Number:', 'Manufacturer:', 'Best Sellers Rank:', 'ASIN:', 'Customer Reviews:', 'Bullets:', 'Manufacturer recommended age:']

    parsed_records = []
    image_index_offset = 0

    for rec in records:
        txt = rec['text']
        asin = rec['asin']
        
        # Parse sections
        csi_txt = get_section(txt, "[CSI DATA]", ["[DP DATA]", "[POWERCHAT REASON]", "[NOVA VISION REASON]", "[IMAGES:"])
        dp_txt = get_section(txt, "[DP DATA]", ["[POWERCHAT REASON]", "[NOVA VISION REASON]", "[IMAGES:"])
        pw_txt = get_section(txt, "[POWERCHAT REASON]", ["[NOVA VISION REASON]", "[IMAGES:"])
        nv_txt = get_section(txt, "[NOVA VISION REASON]", ["[IMAGES:"])
        
        # Parse sub-fields
        csi_fields = parse_keys_flexible(csi_txt, csi_keys)
        dp_fields = parse_keys_flexible(dp_txt, dp_keys)
        
        # Parse powerchat and nova verdicts
        pw_verdict = rec['verdict']
        pw_reason = pw_txt
        pw_verdict_match = re.search(r'Verdict:\s*(TP|FP)', pw_txt, re.IGNORECASE)
        if pw_verdict_match:
            pw_verdict = pw_verdict_match.group(1).upper()
            pw_reason = pw_txt[pw_verdict_match.end():].strip()
            
        nv_verdict = 'N/A'
        nv_reason = nv_txt
        nv_verdict_match = re.search(r'Verdict:\s*(TP|FP)', nv_txt, re.IGNORECASE)
        if nv_verdict_match:
            nv_verdict = nv_verdict_match.group(1).upper()
            nv_reason = nv_txt[nv_verdict_match.end():].strip()
        
        # Find images count
        images_count = 0
        img_match = re.search(r'\[IMAGES:\s*(\d+)\]', txt)
        if img_match:
            images_count = int(img_match.group(1))
            
        # Extract images for this ASIN
        asin_images = []
        for j in range(images_count):
            img_idx = image_index_offset + j
            if img_idx < len(unique_images_data):
                img_data = unique_images_data[img_idx]
                img_filename = f"{asin}_{j}.jpg"
                img_path = os.path.join("images", img_filename)
                
                # Save the image
                with open(img_path, "wb") as f_img:
                    f_img.write(img_data)
                
                # Use relative URL path for the webpage
                asin_images.append(f"./images/{img_filename}")
                
        # Update the offset for the next record
        image_index_offset += images_count
        
        # Clean up reason strings
        pw_reason = clean_spaced_text(pw_reason)
        nv_reason = clean_spaced_text(nv_reason)
        
        # Construct final record dictionary
        record_dict = {
            'asin': asin,
            'csi_title': csi_fields.get('item_name') or dp_fields.get('title') or "No title extracted",
            'csi_status': csi_fields.get('status') or "Active",
            'attributes': {
                'size_info': {
                    'size': csi_fields.get('size'),
                    'size_map': None,
                    'additional_sizes_detected': [],
                    'structured_size': None
                },
                'footwear_size': None,
                'age_range': {
                    'raw_values': [csi_fields.get('age_range_description')] if csi_fields.get('age_range_description') else [],
                    'normalized': None
                },
                'target_gender': csi_fields.get('target_gender'),
                'material': {
                    'raw': csi_fields.get('material'),
                    'normalized': csi_fields.get('material')
                },
                'bullet_points': {
                    'fr': [csi_fields.get('bullet_point')] if csi_fields.get('bullet_point') else [],
                    'en': [dp_fields.get('bullets')] if dp_fields.get('bullets') else [],
                    'other': []
                }
            },
            'dp': {
                'image': asin_images[0] if asin_images else None,
                'images': asin_images,
                'fr_dp_url': f"https://www.amazon.fr/dp/{asin}",
                'title': dp_fields.get('title'),
                'brand_name': dp_fields.get('brand_name'),
                'included_components': dp_fields.get('included_components'),
                'country_of_origin': dp_fields.get('country_of_origin'),
                'unit_count': dp_fields.get('unit_count'),
                'item_type_name': dp_fields.get('item_type_name'),
                'model_name': dp_fields.get('model_name'),
                'model_number': dp_fields.get('model_number'),
                'manufacturer_part_number': dp_fields.get('manufacturer_part_number'),
                'manufacturer': dp_fields.get('manufacturer'),
                'best_sellers_rank': dp_fields.get('best_sellers_rank'),
                'asin': dp_fields.get('asin'),
                'bullets': dp_fields.get('bullets'),
                'manufacturer_recommended_age': dp_fields.get('manufacturer_recommended_age')
            },
            'powerchat_verdict': pw_verdict,
            'powerchat_reason': pw_reason,
            'nova_verdict': nv_verdict,
            'nova_reason': nv_reason
        }
        
        parsed_records.append(record_dict)

    # Write output JSON
    output_data = {
        'source_file': os.path.basename(pdf_path),
        'records': parsed_records
    }

    with open(json_output_path, 'w', encoding='utf-8') as f_json:
        json.dump(output_data, f_json, indent=2, ensure_ascii=False)

    print(f"Successfully wrote JSON dataset to: {json_output_path}")
    print(f"Total parsed records: {len(parsed_records)}")

def main():
    # Find all PDFs in csv folders
    folders = ['p1', 'p2', 'p3']
    for folder in folders:
        pdf_files = glob.glob(f"csv/{folder}/*.pdf")
        for pdf in pdf_files:
            # e.g., csv/p1/DPX_AI_..._Part1.pdf -> out_json/p1_part1.json
            part_match = re.search(r'Part(\d+)', pdf, re.IGNORECASE)
            part_num = part_match.group(1) if part_match else "1"
            out_name = f"{folder}_part{part_num}.json"
            out_path = os.path.join("out_json", out_name)
            
            try:
                convert_pdf_file(pdf, out_path)
            except Exception as e:
                print(f"ERROR converting {pdf}: {e}")

if __name__ == "__main__":
    main()
