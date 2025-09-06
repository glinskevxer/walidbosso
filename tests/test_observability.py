import io
import json
import logging
import threading

import pytest

from binyard.errors import ValidationError
from binyard.observability import Registry, bind, bound, clear, configure, get_logger
from binyard.observability.logging import PlainFormatter, current


@pytest.fixture(autouse=True)
def _clean_context():
    clear()
    yield
    clear()
    logging.getLogger().handlers.clear()


def captured(json_output=True, level="INFO"):
    buffer = io.StringIO()
    configure(level, json_output=json_output, stream=buffer)
    return buffer


class TestLogging:
    def test_a_line_is_one_json_object(self):
        buffer = captured()
        get_logger("binyard.test").info("load received", bushels="857.12")
        payload = json.loads(buffer.getvalue().strip())
        assert payload["event"] == "load received"
        assert payload["level"] == "info"
        assert payload["bushels"] == "857.12"

    def test_bound_context_lands_on_every_line(self):
        buffer = captured()
        with bound(ticket="WBS-25-000001"):
            get_logger("binyard.test").info("graded")
            get_logger("binyard.other").info("binned")
        lines = [json.loads(line) for line in buffer.getvalue().splitlines()]
        assert all(line["ticket"] == "WBS-25-000001" for line in lines)

    def test_context_is_put_back_when_the_block_ends(self):
        buffer = captured()
        with bound(ticket="A"):
            with bound(ticket="B"):
                get_logger("binyard.test").info("inner")
            get_logger("binyard.test").info("outer")
        lines = [json.loads(line) for line in buffer.getvalue().splitlines()]
        assert [line["ticket"] for line in lines] == ["B", "A"]
        assert current() == {}

    def test_bind_lasts_until_it_is_cleared(self):
        buffer = captured()
        bind(location="WBS")
        get_logger("binyard.test").info("one")
        clear()
        get_logger("binyard.test").info("two")
        lines = [json.loads(line) for line in buffer.getvalue().splitlines()]
        assert lines[0]["location"] == "WBS"
        assert "location" not in lines[1]

    def test_a_field_on_the_call_beats_the_context(self):
        buffer = captured()
        with bound(ticket="A"):
            get_logger("binyard.test").info("moved", ticket="B")
        assert json.loads(buffer.getvalue().strip())["ticket"] == "B"

    def test_reserved_names_cannot_be_overwritten(self):
        buffer = captured()
        get_logger("binyard.test").info("hello", name="not the logger", message="nope")
        payload = json.loads(buffer.getvalue().strip())
        assert payload["logger"] == "binyard.test"
        assert payload["event"] == "hello"

    def test_an_exception_carries_its_traceback(self):
        buffer = captured()
        try:
            raise ValueError("bad ticket")
        except ValueError:
            get_logger("binyard.test").exception("failed")
        payload = json.loads(buffer.getvalue().strip())
        assert "ValueError" in payload["error"]

    def test_a_level_below_the_threshold_is_dropped(self):
        buffer = captured(level="WARNING")
        get_logger("binyard.test").info("quiet")
        get_logger("binyard.test").warning("loud")
        assert len(buffer.getvalue().splitlines()) == 1

    def test_non_json_values_are_stringified(self):
        buffer = captured()
        get_logger("binyard.test").info("odd", value=object())
        assert "object object" in buffer.getvalue()

    def test_the_plain_formatter_reads_like_a_line(self):
        buffer = captured(json_output=False)
        with bound(ticket="WBS-25-000001"):
            get_logger("binyard.test").warning("stale tare")
        line = buffer.getvalue().strip()
        assert "stale tare" in line
        assert "ticket=WBS-25-000001" in line
        assert isinstance(logging.getLogger().handlers[0].formatter, PlainFormatter)

    def test_configuring_twice_does_not_double_the_output(self):
        captured()
        buffer = captured()
        get_logger("binyard.test").info("once")
        assert len(buffer.getvalue().splitlines()) == 1


class TestMetrics:
    def test_counters_add_up(self):
        registry = Registry()
        registry.counter("loads_total")
        registry.counter("loads_total", 3)
        assert registry.value_of("loads_total") == 4

    def test_labels_keep_series_apart(self):
        registry = Registry()
        registry.counter("loads_total", commodity="corn")
        registry.counter("loads_total", commodity="soybeans")
        assert registry.value_of("loads_total", commodity="corn") == 1

    def test_a_gauge_replaces_rather_than_adds(self):
        registry = Registry()
        registry.gauge("bin_bushels", 1000)
        registry.gauge("bin_bushels", 900)
        assert registry.value_of("bin_bushels") == 900

    def test_timing_records_an_observation(self):
        registry = Registry()
        with registry.timed("blend_seconds"):
            pass
        assert registry.value_of("blend_seconds") == 1

    def test_asking_for_a_metric_that_is_not_there(self):
        with pytest.raises(ValidationError):
            Registry().value_of("nothing")

    def test_a_bad_metric_name_is_refused(self):
        registry = Registry()
        with pytest.raises(ValidationError):
            registry.counter("Loads Total")
        with pytest.raises(ValidationError):
            registry.counter("")

    def test_a_bad_label_name_is_refused(self):
        with pytest.raises(ValidationError):
            Registry().counter("loads_total", **{"Commodity Code": "corn"})

    def test_rendering_is_the_prometheus_text_format(self):
        registry = Registry()
        registry.counter("loads_total", commodity="corn")
        registry.gauge("bin_bushels", 1000, bin="N1")
        registry.observe("blend_seconds", 0.02)
        rendered = registry.render()
        assert 'loads_total{commodity="corn"} 1' in rendered
        assert 'bin_bushels{bin="N1"} 1000' in rendered
        assert 'blend_seconds_bucket{le="0.05"} 1' in rendered
        assert "blend_seconds_count 1" in rendered

    def test_buckets_are_cumulative(self):
        registry = Registry()
        registry.observe("blend_seconds", 0.002)
        registry.observe("blend_seconds", 0.4)
        rendered = registry.render()
        assert 'blend_seconds_bucket{le="0.005"} 1' in rendered
        assert 'blend_seconds_bucket{le="0.5"} 2' in rendered
        assert 'blend_seconds_bucket{le="+Inf"} 2' in rendered

    def test_an_observation_past_the_last_bucket_still_counts(self):
        registry = Registry()
        registry.observe("blend_seconds", 30.0)
        rendered = registry.render()
        assert 'blend_seconds_bucket{le="5"} 0' in rendered
        assert 'blend_seconds_bucket{le="+Inf"} 1' in rendered

    def test_an_empty_registry_renders_to_nothing(self):
        assert Registry().render() == ""

    def test_label_values_are_escaped(self):
        registry = Registry()
        registry.counter("loads_total", account='HALVORSEN "THE" FARM')
        assert '\\"THE\\"' in registry.render()

    def test_samples_come_back_sorted_and_typed(self):
        registry = Registry()
        registry.gauge("b_gauge", 1)
        registry.counter("a_counter", 2)
        kinds = {sample.name: sample.kind for sample in registry.samples()}
        assert kinds == {"a_counter": "counter", "b_gauge": "gauge"}

    def test_reset_empties_everything(self):
        registry = Registry()
        registry.counter("loads_total")
        registry.reset()
        assert registry.render() == ""

    def test_counting_from_several_threads_loses_nothing(self):
        registry = Registry()

        def work():
            for _ in range(500):
                registry.counter("loads_total")

        threads = [threading.Thread(target=work) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert registry.value_of("loads_total") == 2000
