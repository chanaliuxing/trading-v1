import pytest

from conftest import TestSafetyError, assert_no_live_ibkr_endpoint


@pytest.mark.asyncio
async def test_unit_tests_block_ibkr_connect_async_by_default():
    from ib_insync import IB

    ib = IB()
    with pytest.raises(AssertionError, match="Unit tests must not connect to live IBKR"):
        await ib.connectAsync("127.0.0.1", 4002, clientId=9101)


def test_unit_tests_block_ibkr_connect_sync_by_default():
    from ib_insync import IB

    ib = IB()
    with pytest.raises(AssertionError, match="Unit tests must not connect to live IBKR"):
        ib.connect("127.0.0.1", 4001, clientId=9102)


def test_unit_tests_block_project_ib_adapter_live_port():
    with pytest.raises(TestSafetyError, match="Unit tests must not connect to live IBKR"):
        assert_no_live_ibkr_endpoint("localhost", 4002)


@pytest.mark.asyncio
async def test_unit_test_fake_adapter_allowed():
    class FakeIBAdapter:
        async def connectAsync(self, *_args, **_kwargs):
            return "fake-connected"

        def connect(self, *_args, **_kwargs):
            return "fake-connected"

    fake = FakeIBAdapter()
    assert await fake.connectAsync("127.0.0.1", 4002, clientId=9103) == "fake-connected"
    assert fake.connect("127.0.0.1", 4001, clientId=9104) == "fake-connected"
