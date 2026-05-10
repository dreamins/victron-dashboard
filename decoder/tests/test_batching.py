import pytest
import threading
import time
import collections
from unittest.mock import MagicMock, patch
from decoder import ResilientInfluxClient, VictronPacket, datetime, timezone

def test_batch_writing():
    # Mock InfluxDB components
    with patch('decoder.InfluxDBClient') as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_write_api = mock_client.write_api.return_value
        
        # Instantiate client (starts writer thread)
        influx_client = ResilientInfluxClient()
        
        # Create some dummy packets
        p1 = VictronPacket("dev1", "Label 1", "site1", datetime.now(timezone.utc), {"pv_power": 10.0})
        p2 = VictronPacket("dev2", "Label 2", "site1", datetime.now(timezone.utc), {"pv_power": 20.0})
        
        # Write them to the buffer
        influx_client.write(p1)
        influx_client.write(p2)
        
        # Wait for the writer loop to process (it has a wait or immediate trigger)
        # We need to give it a moment to drain the buffer
        time.sleep(1)
        
        # Verify write was called with a list of points
        assert mock_write_api.write.called
        args, kwargs = mock_write_api.write.call_args
        records = kwargs.get('record') or args[1] # Depending on how it's called
        
        assert isinstance(records, list)
        assert len(records) >= 2
        # Check that our fields are in there (Point objects are complex, but we can check if write was called)
        print(f"Batch size recorded: {len(records)}")

def test_batch_retry_logic():
    with patch('decoder.InfluxDBClient') as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_write_api = mock_client.write_api.return_value
        
        # Fail the first 2 times, then succeed
        mock_write_api.write.side_effect = [Exception("Fail 1"), Exception("Fail 2"), None]
        
        influx_client = ResilientInfluxClient()
        p = VictronPacket("dev1", "Label 1", "site1", datetime.now(timezone.utc), {"pv_power": 10.0})
        
        # Adjust retry delays for fast test
        with patch('decoder.RETRY_DELAYS', [0.1, 0.1]):
            influx_client.write(p)
            time.sleep(1)
            
            # Should have called write 3 times (1 original + 2 retries)
            assert mock_write_api.write.call_count == 3
