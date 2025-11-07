"""
K2 Protocol Codec for ELRO Connects
Based on decompiled Android app ByteUtil.java

The K2 protocol uses simple XOR encryption:
- Encryption: JSON → UTF-8 → Hex → XOR with (random_key ^ 0x23)
- Decryption: Bytes → XOR with (first_byte ^ 0x23) → Hex → UTF-8 → JSON
"""

import json
import random
from typing import Union, Optional


class K2Codec:
    """K2 protocol encoder/decoder for ELRO Connects hub"""
    
    # XOR constant from Android app (CMD_REQUEST_OTA_RES)
    XOR_CONSTANT = 0x23
    
    @staticmethod
    def encode_k2_message(json_data: Union[dict, str]) -> bytes:
        """
        Encode a JSON message to K2 binary format
        
        Args:
            json_data: Dictionary or JSON string to encode
            
        Returns:
            Encrypted bytes ready to send
            
        Example:
            >>> msg = {"msgId": 1, "action": "syncDevStatus"}
            >>> encoded = K2Codec.encode_k2_message(msg)
        """
        # Convert to JSON string if dict
        if isinstance(json_data, dict):
            json_str = json.dumps(json_data, separators=(',', ':'))
        else:
            json_str = json_data
            
        # Convert to UTF-8 bytes, then to hex string
        utf8_bytes = json_str.encode('utf-8')
        hex_string = utf8_bytes.hex()
        
        # Generate random XOR key (0-255)
        xor_key = random.randint(0, 255)
        
        # XOR mask is: key ^ 0x23
        xor_mask = xor_key ^ K2Codec.XOR_CONSTANT
        
        # Build encrypted byte array
        # First byte is the key, followed by encrypted data
        encrypted = bytearray([xor_key])
        
        # Encrypt each hex byte pair
        for i in range(0, len(hex_string), 2):
            hex_byte = int(hex_string[i:i+2], 16)
            encrypted_byte = hex_byte ^ xor_mask
            encrypted.append(encrypted_byte)
            
        return bytes(encrypted)
    
    @staticmethod
    def decode_k2_message(data: bytes) -> Optional[dict]:
        """
        Decode K2 binary format to JSON
        
        Args:
            data: Encrypted bytes received from hub
            
        Returns:
            Decoded JSON as dictionary, or None if invalid
            
        Example:
            >>> data = b'\\x5c<d=~|kvpq...'
            >>> decoded = K2Codec.decode_k2_message(data)
            >>> print(decoded['action'])
        """
        if not data or len(data) < 2:
            return None
            
        try:
            # First byte is the XOR key
            xor_key = data[0]
            
            # XOR mask is: key ^ 0x23
            xor_mask = xor_key ^ K2Codec.XOR_CONSTANT
            
            # Decrypt each byte
            hex_parts = []
            for i in range(1, len(data)):
                decrypted_byte = data[i] ^ xor_mask
                # Convert to 2-digit hex string
                hex_parts.append(f'{decrypted_byte:02x}')
            
            # Join to hex string
            hex_string = ''.join(hex_parts)
            
            # Convert hex to bytes, then to UTF-8 string
            decoded_bytes = bytes.fromhex(hex_string)
            json_str = decoded_bytes.decode('utf-8')
            
            # Handle multiple JSON objects in response
            # (Android app looks for "}}" or "}" to truncate)
            if '}}' in json_str:
                json_str = json_str[:json_str.index('}}') + 2]
            elif '}' in json_str:
                json_str = json_str[:json_str.index('}') + 1]
            
            # Parse JSON
            return json.loads(json_str)
            
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as e:
            print(f"K2 decode error: {e}")
            return None
    
    @staticmethod
    def is_k2_message(data: bytes) -> bool:
        """
        Check if data is likely a K2 encrypted message
        
        K2 messages don't start with '{' like K1 JSON messages
        
        Args:
            data: Raw bytes from hub
            
        Returns:
            True if likely K2 format
        """
        if not data or len(data) < 2:
            return False
            
        # K1 messages start with '{' (0x7B)
        # K2 messages are encrypted, so first byte is random key
        # and second byte won't be recognizable ASCII
        
        # Try to decode as UTF-8 - K1 will succeed, K2 will fail
        try:
            text = data.decode('utf-8')
            # If it decodes and starts with '{', it's K1
            return not text.startswith('{')
        except UnicodeDecodeError:
            # Can't decode as UTF-8, likely K2 binary
            return True


# Testing functions
def test_codec():
    """Test K2 encoding/decoding"""
    
    print("=" * 70)
    print("K2 Codec Test")
    print("=" * 70)
    
    # Test 1: Encode and decode a simple message
    test_msg = {
        "msgId": 1234,
        "action": "syncDevStatus",
        "params": {
            "devTid": "ST_test123"
        }
    }
    
    print("\n1. Original message:")
    print(json.dumps(test_msg, indent=2))
    
    encoded = K2Codec.encode_k2_message(test_msg)
    print(f"\n2. Encoded ({len(encoded)} bytes):")
    print(f"   Hex: {encoded.hex()}")
    print(f"   First byte (key): 0x{encoded[0]:02x} ({encoded[0]})")
    
    decoded = K2Codec.decode_k2_message(encoded)
    print("\n3. Decoded:")
    print(json.dumps(decoded, indent=2))
    
    print("\n4. Verification:")
    print(f"   Match: {test_msg == decoded}")
    
    # Test 2: Decode the real K2 response from previous chat
    print("\n" + "=" * 70)
    print("Testing with real K2 data from hub")
    print("=" * 70)
    
    # This is the actual K2 response you captured
    k2_hex = "3c643d7e7c6b7670713d253d51505b5a404c5a515b3d333d7b7a69565b3d253d4c4b402d2c2b2d2b2f2f282d2d3d333d726c783d25643d726c7840565b3d252a29333d5c525b405c505b5a3d252e26333d7b7e6b7e406c6b6d2e3d253d2f2f2f2e2f2f2f2c2f2f3d333d7b7e6b7e406c6b6d2d3d253d2f2b292b5e5e2f2f3d333d7b7e6b7e406c6b6d2c3d253d3d626215"
    k2_bytes = bytes.fromhex(k2_hex)
    
    print(f"\nK2 data ({len(k2_bytes)} bytes):")
    print(f"Key byte: 0x{k2_bytes[0]:02x} ({k2_bytes[0]})")
    
    decoded_k2 = K2Codec.decode_k2_message(k2_bytes)
    if decoded_k2:
        print("\nDecoded K2 message:")
        print(json.dumps(decoded_k2, indent=2))
    else:
        print("\n❌ Failed to decode K2 message")


if __name__ == "__main__":
    test_codec()
