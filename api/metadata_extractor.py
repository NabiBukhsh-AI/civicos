from typing import Dict, List
import exifread
import io


class MetadataExtractor:
    @staticmethod
    def convert_coordinates(coord: list, ref: str) -> float:
        """
        Convert GPS coordinates (degrees, minutes, seconds) to decimal degrees.
        """
        try:
            if len(coord) != 3:
                raise ValueError(f"Invalid coordinate format: {coord}")

            degrees = float(coord[0].num) / float(coord[0].den) if hasattr(coord[0], 'num') else float(coord[0])
            minutes = float(coord[1].num) / float(coord[1].den) if hasattr(coord[1], 'num') else float(coord[1])
            seconds = float(coord[2].num) / float(coord[2].den) if hasattr(coord[2], 'num') else float(coord[2])

            decimal_degrees = degrees + (minutes / 60) + (seconds / 3600)
            return decimal_degrees if ref in ['N', 'E'] else -decimal_degrees
        except Exception as e:
            print(f"[ERROR] Converting coordinates {coord} with ref {ref}: {str(e)}")
            return None

    @classmethod
    def extract_metadata_list(cls, images: List[tuple[bytes, str]]) -> List[Dict]:
        metadata_list = []

        for index, (image_bytes, filename) in enumerate(images, 1):
            metadata = {"index": index, "filename": filename}
            try:
                with io.BytesIO(image_bytes) as file_buffer:
                    tags = exifread.process_file(file_buffer, details=True)

                if not tags:
                    metadata["error"] = "No EXIF data found"
                else:
                    # DateTime
                    dt_tag = tags.get('EXIF DateTimeOriginal') or tags.get('EXIF DateTime')
                    if dt_tag:
                        metadata['datetime'] = str(dt_tag)

                    # Device info
                    if 'EXIF LensModel' in tags:
                        metadata['device'] = str(tags['EXIF LensModel'])

                    # GPS Info
                    latitude = None
                    longitude = None

                    lat_tag = tags.get('GPS GPSLatitude')
                    lat_ref = tags.get('GPS GPSLatitudeRef')
                    lon_tag = tags.get('GPS GPSLongitude')
                    lon_ref = tags.get('GPS GPSLongitudeRef')

                    if lat_tag and lat_ref:
                        latitude = cls.convert_coordinates(lat_tag.values, str(lat_ref))

                    if lon_tag and lon_ref:
                        longitude = cls.convert_coordinates(lon_tag.values, str(lon_ref))

                    if latitude is not None:
                        metadata['latitude'] = latitude
                    else:
                        metadata['error'] = metadata.get('error', '') + "Failed to extract latitude. "

                    if longitude is not None:
                        metadata['longitude'] = longitude
                    else:
                        metadata['error'] = metadata.get('error', '') + "Failed to extract longitude. "

                    if latitude is None and longitude is None:
                        metadata['error'] = metadata.get('error', '') + "No valid GPS coordinates found."

            except Exception as e:
                metadata["error"] = f"Exception: {str(e)}"
                print(f"[ERROR] Processing {filename}: {str(e)}")

            metadata_list.append(metadata)

        return metadata_list
