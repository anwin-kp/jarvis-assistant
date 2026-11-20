from jarvis.tts.sapi_tts import speakable


def test_speakable_cleans_markdown_and_units():
    s = speakable("**Kochi** is 29°C, humidity 70%. See [BBC](https://bbc.com) or https://x.y/z")
    assert s == "Kochi is 29 degrees Celsius, humidity 70 percent. See BBC or a link"
