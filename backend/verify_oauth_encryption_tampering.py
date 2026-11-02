from app.core.security import encrypt_data, decrypt_data


def verify_tampered_ciphertext():
    token = "dummy-google-token"

    encrypted = encrypt_data(token)

    # Change part of the encrypted ciphertext.
    tampered = encrypted[:-4] + (
        "AAAA" if encrypted[-4:] != "AAAA" else "BBBB"
    )

    try:
        decrypt_data(tampered)
    except ValueError:
        print("Tampered ciphertext protection: PASS")
        return

    raise AssertionError("Tampered ciphertext was accepted")


if __name__ == "__main__":
    verify_tampered_ciphertext()