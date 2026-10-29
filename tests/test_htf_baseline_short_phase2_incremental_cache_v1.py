import tempfile
import unittest
from pathlib import Path

from app.models import Candle
from app.research.htf_baseline_short_phase2_contracts_v1 import BAR_MS
from app.research.htf_baseline_short_phase2_incremental_cache_v1 import (
    IncrementalPhase2CandleSource,
)

BAR = BAR_MS
from app.research.htf_baseline_short_phase2_incremental_cache_v1 import CacheIntegrityError

x = type("CacheModule", (), {
    "IncrementalPhase2CandleSource": IncrementalPhase2CandleSource,
    "CacheIntegrityError": CacheIntegrityError,
})

class FakeUpstream:
    page_limit=200
    max_attempts=3
    min_history_bars=12
    def __init__(self):
        self.now=BAR*101
        self.calls=0
        self.last_evaluation_asof_ms=None
        self.last_server_time_ms=None
        self.last_load_metrics={'pages_fetched':1}
    def load(self,symbol,*,resume_open_ms=None):
        self.calls+=1
        self.last_evaluation_asof_ms=self.now
        self.last_server_time_ms=self.now
        return [Candle(BAR*i,10,11,9,10,1) for i in range(89,101)]
    def _fetch_server_time_ms(self):return self.now
    def _resolve_asof(self,ms):return ms
    def _fetch_page(self,symbol,start,end,*,window_end,attempts):
        self.calls+=1
        return [[str(t), '10', '11', '9', '10', '1'] for t in range(start,end+BAR,BAR)],self.now
    def _merge_rows(self,symbol,rows,start,end,asof,by_open,unclosed):
        for row in rows:
            t=int(row[0]);by_open[t]=Candle(t,*map(float,row[1:]))
        return 0

class Tests(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory();self.addCleanup(self.dir.cleanup)
        self.up=FakeUpstream()
        self.source=x.IncrementalPhase2CandleSource(self.up,self.dir.name,max_incremental_bars=10,max_cache_bars=22)
    def test_cold_and_incremental_no_redundant_download(self):
        self.assertEqual(len(self.source.load('BTCUSDT')),12)
        self.assertEqual(self.up.calls,1)
        self.assertEqual(len(self.source.load('BTCUSDT')),12)
        self.assertEqual(self.up.calls,1)
        self.up.now+=BAR
        self.assertEqual(len(self.source.load('BTCUSDT')),13)
        self.assertEqual(self.up.calls,2)
    def test_checksum_fail_closed(self):
        self.source.load('BTCUSDT')
        p=Path(self.dir.name)/'BTCUSDT.json'
        p.write_text(p.read_text().replace('"open"','"zz"')+' ')
        # Explicitly alter serialized value to invalidate checksum.
        p.write_text(p.read_text().replace('"BTCUSDT"','"ETHUSDT"'))
        with self.assertRaises(x.CacheIntegrityError):self.source.load('BTCUSDT')
    def test_long_gap_blocks_without_clobbering_cache(self):
        self.source.load('BTCUSDT')
        old=(Path(self.dir.name)/'BTCUSDT.json').read_bytes()
        self.up.now+=BAR*12
        with self.assertRaises(x.CacheIntegrityError):self.source.load('BTCUSDT')
        self.assertEqual((Path(self.dir.name)/'BTCUSDT.json').read_bytes(),old)
    def test_invalid_cursor_fails_closed(self):
        self.source.load('BTCUSDT')
        with self.assertRaises(x.CacheIntegrityError): self.source.load('BTCUSDT',resume_open_ms=1)
    def test_reopen_persistent_cache(self):
        self.source.load('BTCUSDT')
        second=x.IncrementalPhase2CandleSource(self.up,self.dir.name,max_incremental_bars=10,max_cache_bars=22)
        second.load('BTCUSDT')
        self.assertEqual(self.up.calls,1)

if __name__=='__main__':unittest.main()
