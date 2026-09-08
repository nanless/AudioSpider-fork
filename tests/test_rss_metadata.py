import unittest

from bs4 import BeautifulSoup

from background import decode_metadata
from rss_metadata import apply_rss_metadata
from storage import AudioRecord


class RSSMetadataTests(unittest.TestCase):
    def test_extracts_descriptions_people_license_and_assets(self):
        xml = """
        <rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
             xmlns:content="http://purl.org/rss/1.0/modules/content/"
             xmlns:podcast="https://podcastindex.org/namespace/1.0">
          <channel>
            <title>Example Show</title>
            <description>Show description</description>
            <itunes:author>Show Author</itunes:author>
            <itunes:image href="https://cdn.example/show.jpg"/>
            <podcast:person role="host" href="https://example/host">Host Name</podcast:person>
            <podcast:license url="https://example/license">cc-by-4.0</podcast:license>
            <item>
              <title>Episode</title>
              <link>https://example/episode</link>
              <content:encoded><![CDATA[<p>Hello <b>world</b></p>]]></content:encoded>
              <podcast:transcript url="transcript.vtt" type="text/vtt" language="en"/>
              <podcast:chapters url="chapters.json" type="application/json+chapters"/>
            </item>
          </channel>
        </rss>
        """
        soup = BeautifulSoup(xml, "lxml-xml")
        record = apply_rss_metadata(
            AudioRecord(url="https://cdn.example/audio.mp3", source="podcast_rss"),
            soup.find("channel"), soup.find("item"), "https://feed.example/rss.xml",
        )
        metadata = decode_metadata(record.metadata_json)
        self.assertEqual(record.description, "Hello world")
        self.assertEqual(record.webpage_url, "https://example/episode")
        self.assertEqual(record.author, "Show Author")
        self.assertEqual(record.cover_url, "https://cdn.example/show.jpg")
        self.assertEqual(metadata["common"]["people"][0]["name"], "Host Name")
        self.assertEqual(metadata["common"]["license"]["name"], "cc-by-4.0")
        self.assertEqual(
            metadata["assets"]["transcripts"][0]["url"],
            "https://feed.example/transcript.vtt",
        )
        self.assertEqual(
            metadata["assets"]["chapters"][0]["url"],
            "https://feed.example/chapters.json",
        )


if __name__ == "__main__":
    unittest.main()
