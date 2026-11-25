"""Probe UA calls the answering UA directly over localhost: tones must be detected both ways."""
import asyncio

from notrufmon.ua import Account, UserAgent


def _ua(user, port_min, **kw):
    return UserAgent(Account(username=user, password="", domain="127.0.0.1", rtp_port_min=port_min,
                             rtp_port_max=port_min + 50, keepalive_s=0, **kw), name=user)


def test_direct_call_tones_both_ways():
    async def run():
        results = {}
        done = asyncio.Event()

        async def on_invite(ua, req, addr):
            results["server"] = await ua.answer(req, addr, answer_delay=0.05)
            done.set()

        srv = UserAgent(Account(username="trunk", password="", domain="127.0.0.1", port=1,
                                rtp_port_min=31000, rtp_port_max=31050, keepalive_s=0),
                        on_invite=on_invite, name="srv")
        await srv.start()
        cli = _ua("4711", 31200, port=srv.local_port)
        await cli.start()
        res = await cli.call("112", 6.0)
        await asyncio.wait_for(done.wait(), 5)
        await cli.close(unregister=False)
        await srv.close(unregister=False)
        return res, results["server"]

    res, srv = asyncio.run(run())
    assert res["success"] and res["sip_code"] == 200 and res["bye_by"] == "local"
    assert res["rx"]["audio_ok"], res["rx"]          # server -> probe
    assert srv["rx"]["audio_ok"], srv["rx"]          # probe -> server
    assert srv["from_user"] == "4711"
    assert res["rx"]["first_tone_s"] < 1.0
