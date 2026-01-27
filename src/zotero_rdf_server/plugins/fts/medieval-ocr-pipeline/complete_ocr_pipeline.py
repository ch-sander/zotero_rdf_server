#!/usr/bin/env python3
"""
Medieval OCR Text Correction Pipeline using ByT5 and Kraken
==========================================================

A complete pipeline for processing medieval manuscript images through:
1. Kraken-based line segmentation
2. TrOCR medieval text recognition  
3. ByT5-based OCR error correction

This implementation supports the research published in:
"Modular Pipeline for Text Recognition in Early Printed Books Using Kraken and ByT5"
Electronics 2025, 14(15), 3083; https://doi.org/10.3390/electronics14153083

Authors: Yahya Momtaz, Lorenza Laccetti and Guido Russo
Institution: University of Naples Federico II
Email: yahya.momtaz@unina.it

Usage:
    python complete_ocr_pipeline.py --image_path dataset/images/manuscript.jpg
    
Requirements:
    - Python 3.12
    - CUDA-capable GPU (recommended)
    - Kraken OCR engine
    - Fine-tuned ByT5 correction model
"""

import os
import subprocess
import json
import argparse
import logging
import shutil
from typing import List, Dict, Tuple, Optional, Any
from pathlib import Path

import torch
from PIL import Image
from transformers import (
    TrOCRProcessor, 
    VisionEncoderDecoderModel, 
    AutoTokenizer, 
    T5ForConditionalGeneration
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Model configuration constants
DEFAULT_OCR_MODEL = "medieval-data/trocr-medieval-print"
MAX_SEQUENCE_LENGTH = 512
GENERATION_BEAM_SIZE = 4
DEVICE_AUTO = 'cuda' if torch.cuda.is_available() else 'cpu'


def setup_models(device: str = DEVICE_AUTO) -> Tuple[Any, Any, Any, Any, str]:
    """
    Initialize and load all required models for the OCR pipeline.
    
    This function loads:
    1. TrOCR processor and model for medieval manuscript recognition
    2. Fine-tuned ByT5 tokenizer and model for OCR error correction
    
    Args:
        device (str): Computing device ('cuda', 'cpu', or 'auto')
        
    Returns:
        Tuple containing:
            - TrOCRProcessor: Image preprocessing for TrOCR
            - VisionEncoderDecoderModel: TrOCR model for initial OCR
            - AutoTokenizer: ByT5 tokenizer for correction model
            - T5ForConditionalGeneration: Fine-tuned ByT5 correction model
            - str: Actual device being used
            
    Raises:
        FileNotFoundError: If correction model is not found
        RuntimeError: If models fail to load
    """
    logger.info(f"Initializing models on device: {device}")
    
    try:
        # Load TrOCR model optimized for medieval manuscripts
        logger.info("Loading TrOCR model for medieval manuscripts...")
        processor = TrOCRProcessor.from_pretrained(DEFAULT_OCR_MODEL)
        ocr_model = VisionEncoderDecoderModel.from_pretrained(DEFAULT_OCR_MODEL)
        ocr_model.to(device)
        logger.info(f"✓ TrOCR model loaded: {DEFAULT_OCR_MODEL}")
        
        # Load fine-tuned ByT5 correction model
        logger.info("Loading fine-tuned ByT5 correction model...")
        correction_model = T5ForConditionalGeneration.from_pretrained("yayamomt/byt5-medieval-ocr-correction")
        tokenizer = AutoTokenizer.from_pretrained("yayamomt/byt5-medieval-ocr-correction")
        correction_model.to(device)
        logger.info(f"✓ ByT5 correction model loaded: yayamomt/byt5-medieval-ocr-correction")
        
        return processor, ocr_model, tokenizer, correction_model, device
        
    except Exception as e:
        logger.error(f"Failed to load models: {e}")
        raise RuntimeError(f"Model initialization failed: {e}")


def segment_image_with_kraken(
    image_path: str, 
    output_dir: str = "temp_segmentation"
) -> Tuple[Optional[Dict], List[Dict]]:
    """
    Segment a manuscript image into individual text lines using Kraken.
    
    Kraken is used for its superior performance on historical documents.
    The segmentation process:
    1. Runs Kraken's baseline detection algorithm
    2. Extracts bounding boxes for each detected line
    3. Crops individual line images for OCR processing
    
    Args:
        image_path (str): Path to the input manuscript image
        output_dir (str): Directory for temporary segmentation files
        
    Returns:
        Tuple containing:
            - Dict: Kraken segmentation metadata (or None if failed)
            - List[Dict]: List of line information dictionaries
            
    Each line dictionary contains:
        - 'image': PIL Image object of the cropped line
        - 'path': File path to saved line image
        - 'line_number': Sequential line number (0-based)
        - 'coords': Original bounding box coordinates
    """
    logger.info(f"Segmenting image: {image_path}")
    
    # Validate input image
    if not os.path.exists(image_path):
        logger.error(f"Image file not found: {image_path}")
        return None, []
    
    # Create output directory
    os.makedirs(output_dir, exist_ok=True)
    
    # Generate segmentation file path
    base_name = os.path.splitext(os.path.basename(image_path))[0]
    segmentation_path = os.path.join(output_dir, f"{base_name}_segmentation.json")
    
    # Run Kraken segmentation if not already done
    if not os.path.exists(segmentation_path):
        logger.info("Running Kraken line segmentation...")
        try:
            # Execute Kraken with baseline detection
            subprocess.run([
                "kraken", "-i", image_path, segmentation_path, "segment", "-bl"
            ], check=True, capture_output=True, text=True)
            logger.info("✓ Kraken segmentation completed")
            
        except subprocess.CalledProcessError as e:
            logger.error(f"Kraken segmentation failed: {e}")
            return None, []
        except FileNotFoundError:
            logger.error("Kraken not found. Please install: pip install kraken")
            return None, []
    else:
        logger.info("Using existing segmentation file")
    
    # Load segmentation results
    try:
        with open(segmentation_path, "r", encoding="utf-8") as f:
            segmentation_data = json.load(f)
    except Exception as e:
        logger.error(f"Failed to load segmentation data: {e}")
        return None, []
    
    # Extract individual line images
    line_images = []
    try:
        image = Image.open(image_path).convert("RGB")
        logger.info(f"Processing {len(segmentation_data.get('lines', []))} detected lines")
        
        for i, line in enumerate(segmentation_data.get("lines", [])):
            # Extract bounding box coordinates
            coords = line["boundary"]
            x_coords = [pt[0] for pt in coords]
            y_coords = [pt[1] for pt in coords]
            
            # Calculate crop boundaries
            left = int(min(x_coords))
            right = int(max(x_coords))
            top = int(min(y_coords))
            bottom = int(max(y_coords))
            
            # Crop line from original image
            cropped_line = image.crop((left, top, right, bottom))
            
            # Save cropped line image
            line_filename = f"{base_name}_{i:02d}.png"
            line_path = os.path.join(output_dir, line_filename)
            cropped_line.save(line_path)
            
            # Store line information
            line_info = {
                'image': cropped_line,
                'path': line_path,
                'line_number': i,
                'coords': coords,
                'bbox': (left, top, right, bottom)
            }
            line_images.append(line_info)
            
            logger.debug(f"Extracted line {i}: {line_filename}")
        
        logger.info(f"✓ Successfully extracted {len(line_images)} lines")
        return segmentation_data, line_images
        
    except Exception as e:
        logger.error(f"Failed to extract line images: {e}")
        return None, []


def perform_ocr_on_line(
    line_image: Image.Image, 
    processor: TrOCRProcessor, 
    ocr_model: VisionEncoderDecoderModel, 
    device: str
) -> str:
    """
    Perform OCR on a single text line using TrOCR.
    
    TrOCR (Text Recognition with Vision Transformers) is specifically
    fine-tuned for medieval manuscripts, making it ideal for historical
    document processing.
    
    Args:
        line_image (PIL.Image): Cropped image of a single text line
        processor (TrOCRProcessor): TrOCR image processor
        ocr_model (VisionEncoderDecoderModel): TrOCR model
        device (str): Computing device
        
    Returns:
        str: Raw OCR text output
        
    Note:
        The model generates text autoregressively using beam search
        for improved accuracy on medieval text patterns.
    """
    try:
        # Preprocess image for TrOCR
        pixel_values = processor(line_image, return_tensors="pt").pixel_values
        pixel_values = pixel_values.to(device)
        
        # Generate OCR text with beam search
        with torch.no_grad():
            generated_ids = ocr_model.generate(
                pixel_values,
                max_length=MAX_SEQUENCE_LENGTH,
                num_beams=GENERATION_BEAM_SIZE,
                early_stopping=True,
                do_sample=False  # Deterministic output
            )
        
        # Decode generated tokens to text
        ocr_text = processor.batch_decode(generated_ids, skip_special_tokens=True)[0]
        return ocr_text.strip()
        
    except Exception as e:
        logger.error(f"OCR failed for line: {e}")
        return ""


def correct_ocr_text(
    ocr_text: str, 
    tokenizer: AutoTokenizer, 
    correction_model: T5ForConditionalGeneration, 
    device: str
) -> str:
    """
    Correct OCR errors using fine-tuned ByT5 model.
    
    ByT5 (Byte-level T5) is particularly effective for OCR correction because:
    1. It operates at the character/byte level
    2. It can handle spelling errors and character substitutions
    3. It's robust to OCR artifacts and special characters
    4. It maintains medieval text characteristics
    
    Args:
        ocr_text (str): Raw OCR output text
        tokenizer (AutoTokenizer): ByT5 tokenizer
        correction_model (T5ForConditionalGeneration): Fine-tuned correction model
        device (str): Computing device
        
    Returns:
        str: Corrected text output
        
    Note:
        The model was fine-tuned on medieval manuscript data with
        OCR error patterns specific to historical documents.
    """
    if not ocr_text.strip():
        return ""
    
    try:
        # Prepare input with task prefix (as used during training)
        input_text = f"correct: {ocr_text}"
        
        # Tokenize input text
        inputs = tokenizer(
            input_text, 
            return_tensors="pt", 
            padding=True, 
            truncation=True, 
            max_length=MAX_SEQUENCE_LENGTH
        )
        inputs = {k: v.to(device) for k, v in inputs.items()}
        
        # Generate corrected text
        with torch.no_grad():
            generated_ids = correction_model.generate(
                inputs["input_ids"],
                max_length=MAX_SEQUENCE_LENGTH,
                num_beams=GENERATION_BEAM_SIZE,
                early_stopping=True,
                do_sample=False  # Deterministic correction
            )
        
        # Decode corrected text
        corrected_text = tokenizer.decode(generated_ids[0], skip_special_tokens=True)
        return corrected_text.strip()
        
    except Exception as e:
        logger.error(f"Text correction failed: {e}")
        return ocr_text  # Return original text if correction fails


def process_complete_image(
    image_path: str, 
    output_file: Optional[str] = None, 
    cleanup_temp: bool = True,
    verbose: bool = True
) -> Optional[Tuple[str, List[Dict]]]:
    """
    Process a complete manuscript image through the entire OCR correction pipeline.
    
    This is the main pipeline function that orchestrates:
    1. Model initialization
    2. Line segmentation with Kraken
    3. OCR processing with TrOCR
    4. Error correction with ByT5
    5. Results compilation and output
    
    Args:
        image_path (str): Path to input manuscript image
        output_file (Optional[str]): Path for saving results
        cleanup_temp (bool): Whether to remove temporary files
        verbose (bool): Whether to print detailed progress
        
    Returns:
        Optional[Tuple[str, List[Dict]]]: 
            - Final corrected text
            - List of per-line processing results
            Returns None if processing fails
            
    Each result dictionary contains:
        - 'line_number': Line position (1-based)
        - 'ocr_text': Raw OCR output
        - 'corrected_text': Error-corrected text
        - 'image_path': Path to line image
        - 'bbox': Bounding box coordinates
    """
    logger.info(f"Starting complete OCR pipeline for: {image_path}")
    
    # Validate input
    if not os.path.exists(image_path):
        logger.error(f"Input image not found: {image_path}")
        return None
    
    try:
        # Initialize models
        processor, ocr_model, tokenizer, correction_model, device = setup_models()
        
        # Perform line segmentation
        segmentation_data, line_images = segment_image_with_kraken(image_path)
        
        if not line_images:
            logger.warning("No text lines detected in image")
            return None
        
        # Process each detected line
        all_results = []
        final_text_lines = []
        
        if verbose:
            print(f"\nProcessing {len(line_images)} lines...")
            print("=" * 60)
        
        for line_info in line_images:
            line_num = line_info['line_number']
            line_image = line_info['image']
            
            if verbose:
                print(f"\nProcessing line {line_num + 1}/{len(line_images)}")
            
            # Perform initial OCR
            ocr_text = perform_ocr_on_line(line_image, processor, ocr_model, device)
            if verbose:
                print(f"OCR Output: {ocr_text}")
            
            # Apply error correction
            corrected_text = correct_ocr_text(ocr_text, tokenizer, correction_model, device)
            if verbose:
                print(f"Corrected:  {corrected_text}")
                print("-" * 40)
            
            # Store processing results
            result = {
                'line_number': line_num + 1,
                'ocr_text': ocr_text,
                'corrected_text': corrected_text,
                'image_path': line_info['path'],
                'bbox': line_info.get('bbox', None)
            }
            all_results.append(result)
            final_text_lines.append(corrected_text)
        
        # Combine all corrected lines
        final_text = '\n'.join(final_text_lines)
        
        if verbose:
            print("\n" + "=" * 60)
            print("FINAL CORRECTED TEXT:")
            print("=" * 60)
            print(final_text)
            print("=" * 60)
        
        # Save results if output file specified
        if output_file:
            save_results(image_path, final_text, all_results, output_file)
        
        # Cleanup temporary files
        if cleanup_temp:
            cleanup_temporary_files()
        
        logger.info(f"✓ Pipeline completed successfully. Processed {len(all_results)} lines")
        return final_text, all_results
        
    except Exception as e:
        logger.error(f"Pipeline processing failed: {e}")
        return None


def save_results(
    image_path: str, 
    final_text: str, 
    all_results: List[Dict], 
    output_file: str
) -> None:
    """
    Save pipeline results to output files.
    
    Creates two output files:
    1. Final corrected text file
    2. Detailed line-by-line results file
    
    Args:
        image_path (str): Original image path
        final_text (str): Complete corrected text
        all_results (List[Dict]): Per-line processing results
        output_file (str): Base output file path
    """
    try:
        base_name = os.path.splitext(os.path.basename(image_path))[0]
        
        # Save final corrected text
        final_text_file = output_file
        with open(final_text_file, 'w', encoding='utf-8') as f:
            f.write(final_text)
        logger.info(f"✓ Final corrected text saved: {final_text_file}")
        
        # Save detailed line-by-line results
        detailed_file = f"{base_name}_detailed_results.txt"
        with open(detailed_file, 'w', encoding='utf-8') as f:
            f.write("DETAILED OCR CORRECTION RESULTS\n")
            f.write("=" * 50 + "\n\n")
            f.write(f"Source Image: {image_path}\n")
            f.write(f"Total Lines: {len(all_results)}\n")
            f.write(f"Processing Device: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU'}\n\n")
            
            for result in all_results:
                f.write(f"Line {result['line_number']}:\n")
                f.write(f"  Original OCR: {result['ocr_text']}\n")
                f.write(f"  Corrected:    {result['corrected_text']}\n")
                f.write(f"  Image:        {result['image_path']}\n")
                if result.get('bbox'):
                    f.write(f"  Bounding Box: {result['bbox']}\n")
                f.write("-" * 30 + "\n")
            
            f.write(f"\nFINAL COMBINED TEXT:\n")
            f.write("=" * 20 + "\n")
            f.write(final_text)
        
        logger.info(f"✓ Detailed results saved: {detailed_file}")
        
    except Exception as e:
        logger.error(f"Failed to save results: {e}")


def cleanup_temporary_files(temp_dir: str = "temp_segmentation") -> None:
    """
    Clean up temporary segmentation files.
    
    Args:
        temp_dir (str): Temporary directory to remove
    """
    try:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
            logger.info("✓ Cleaned up temporary files")
    except Exception as e:
        logger.warning(f"Failed to cleanup temporary files: {e}")


def main():
    """
    Main entry point for the OCR pipeline.
    
    Handles command-line arguments and executes the complete pipeline.
    """
    parser = argparse.ArgumentParser(
        description='Medieval OCR Text Correction Pipeline',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Process single image
  python complete_ocr_pipeline.py --image_path manuscript.jpg
  
  # Save results to specific file
  python complete_ocr_pipeline.py --image_path manuscript.jpg --output_file results.txt
  
  # Keep temporary files for inspection
  python complete_ocr_pipeline.py --image_path manuscript.jpg --keep_temp
        """
    )
    
    parser.add_argument(
        '--image_path', 
        default='dataset/images/0001_001.jpg',
        help='Path to the input manuscript image'
    )
    parser.add_argument(
        '--output_file', 
        help='Output file path for corrected text'
    )
    parser.add_argument(
        '--keep_temp', 
        action='store_true',
        help='Keep temporary segmentation files for inspection'
    )
    parser.add_argument(
        '--quiet', 
        action='store_true',
        help='Suppress verbose output'
    )
    parser.add_argument(
        '--device',
        choices=['auto', 'cuda', 'cpu'],
        default='auto',
        help='Computing device to use'
    )
    
    args = parser.parse_args()
    
    # Set device
    if args.device == 'auto':
        device = DEVICE_AUTO
    else:
        device = args.device
    
    # Validate input image
    if not os.path.exists(args.image_path):
        logger.error(f"Input image not found: {args.image_path}")
        return 1
    
    # Process the image
    result = process_complete_image(
        image_path=args.image_path, 
        output_file=args.output_file,
        cleanup_temp=not args.keep_temp,
        verbose=not args.quiet
    )
    
    if result is not None:
        final_text, results = result
        print(f"\n✓ Processing completed successfully!")
        print(f"  - Processed {len(results)} lines")
        print(f"  - Total characters: {len(final_text)}")
        print(f"  - Average line length: {len(final_text) / len(results):.1f} chars")
        return 0
    else:
        print("✗ Processing failed")
        return 1


if __name__ == "__main__":
    exit(main()) 