from workbench_core.files import MAX_WRITE_BYTES, FileWriteRequest, FileWriteService


def test_file_write_api_is_public() -> None:
    assert MAX_WRITE_BYTES == 64 * 1024
    assert FileWriteRequest.__name__ == "FileWriteRequest"
    assert FileWriteService.__name__ == "FileWriteService"
