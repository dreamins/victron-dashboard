import pytest
import threading
import time
import collections
from unittest.mock import MagicMock, patch
from decoder import ResilientInfluxClient, VictronPacket, datetime, timezone

def test_timed_batch_writing():
    """Verify that notify() is NOT called and writing is deferred."""
    with patch('decoder.InfluxDBClient') as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_write_api = mock_client.write_api.return_value
        
        influx_client = ResilientInfluxClient()
        p1 = VictronPacket("dev1", "L1", "s1", datetime.now(timezone.utc), {"pv_power": 10.0})
        
        # 1. Write packet
        influx_client.write(p1)
        
        # 2. Verify it DOES NOT write immediately
        time.sleep(0.5)
        assert not mock_write_api.write.called
        
        # 3. Manually notify to simulate timeout or wake-up
        with influx_client._cv:
            influx_client._cv.notify()
            
        # 4. Give thread a moment to process
        time.sleep(0.5)
        assert mock_write_api.write.called
        args, kwargs = mock_write_api.write.call_args
        records = kwargs.get('record') or args[1]
        assert len(records) == 1

def test_batch_drain_all():
    """Verify that multiple packets are combined into a single batch."""
    with patch('decoder.InfluxDBClient') as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_write_api = mock_client.write_api.return_value
        
        influx_client = ResilientInfluxClient()
        
        # Add 5 packets
        for i in range(5):
            influx_client.write(VictronPacket(f"d{i}", "L", "s", datetime.now(timezone.utc), {"v": float(i)}))
            
        # Manually wake up the writer
        with influx_client._cv:
            influx_client._cv.notify()
            
        time.sleep(0.5)
        
        # Should have called write ONCE with 5 records
        assert mock_write_api.write.call_count == 1
        args, kwargs = mock_write_api.write.call_args
        records = kwargs.get('record') or args[1]
        assert len(records) == 5

def test_batch_retry_logic():
    """Verify that the entire batch is retried on failure."""
    with patch('decoder.InfluxDBClient') as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_write_api = mock_client.write_api.return_value
        
        # Fail once, then succeed
        mock_write_api.write.side_effect = [Exception("Fail 1"), None]
        
        influx_client = ResilientInfluxClient()
        p = VictronPacket("dev1", "L", "s", datetime.now(timezone.utc), {"v": 1.0})
        
        with patch('decoder.RETRY_DELAYS', [0.1]):
            influx_client.write(p)
            with influx_client._cv:
                influx_client._cv.notify()
            
            time.sleep(0.5)
            
            # Should have called write 2 times
            assert mock_write_api.write.call_count == 2
