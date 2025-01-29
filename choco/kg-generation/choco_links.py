#!/usr/bin/env python3

import hashlib
import json
import os
import sys
from difflib import SequenceMatcher
from json import JSONDecodeError

from rdflib import Graph, URIRef

similarity_ratio = 0.80
midildc_prefix = "https://purl.org/midi-ld/piece/"
choco_prefix = "https://purl.org/choco/data/"
musicbrainz_prefix = "https://musicbrainz.org/recording/"
owl_prefix = "http://www.w3.org/2002/07/owl#"


def midi_choco_links(midi_path, jams_path, links_outfile):
    links = Graph()

    abs_midi_path = os.path.abspath(midi_path)
    abs_jams_path = os.path.abspath(jams_path)
    print("Walking {}".format(abs_midi_path))

    midis = []
    for root, dirs, files in os.walk(abs_midi_path):
        for file in files:
            if ".mid" in file or ".midi" in file:
                midi_file_path = os.path.join(root, file)
                midi_file_name = os.path.splitext(file)[0]
                md5_midi_id = hashlib.md5(open(midi_file_path, 'rb').read()).hexdigest()
                midis.append({'id': md5_midi_id, 'name': midi_file_name})

    print("Walking {}".format(abs_jams_path))

    jams = []
    for root, dirs, files in os.walk(abs_jams_path):
        for file in files:
            choco_path = os.path.join(root, file).split('/')[7]
            # print("Choco path: {}, jams collection: {}".format(choco_path, jams_collection))
            if ".jams" in file and "choco" in choco_path:
                # print(root, file)
                with open(os.path.join(root, file), 'r') as jams_file:
                    try:
                        jams_data = json.load(jams_file)
                        jams_collection = str(os.path.join(root, file)).split('/')[6]
                        # jams_item = str(os.path.join(root,file)).split('/')[-1].split('.')[0]
                        # jams_id = jams_collection + '/' + jams_item
                        jams_id = jams_collection + '/' + file.split('.')[0]
                        jams_name = str(jams_data['file_metadata']['artist']) + " " + str(
                            jams_data['file_metadata']['title'])
                        jams.append({'id': jams_id, 'name': jams_name})

                        # If we have links to MusicBrainz, we add them
                        if 'MB' in jams_data['file_metadata']['identifiers']:
                            s = URIRef(choco_prefix + jams_id.replace(" ", "_"))
                            p = URIRef(owl_prefix + 'sameAs')
                            o = URIRef(musicbrainz_prefix + jams_data['file_metadata']['identifiers']['MB'])
                            links.add((s, p, o))

                    except JSONDecodeError as e:
                        print("Error reading JAMS file {}: {}".format(os.path.join(root, file), e))
                        pass

    print("Writing inherited links from JAMS.file_metadata.identifiers...")
    with open(links_outfile, 'w') as linksfile:
        linksfile.write(links.serialize(format='nt'))

    links = Graph()

    print("Comparing JAMS with MIDI metadata...")

    for midi_i, m in enumerate(midis):
        print("Doing MIDI {} of {}".format(midi_i, len(midis)))
        for j in jams:
            if SequenceMatcher(None, m['name'], j['name']).ratio() > similarity_ratio:
                # print("{} || {}".format(m['name'], j['name']))
                s = URIRef(midildc_prefix + m['id'])
                p = URIRef(owl_prefix + 'sameAs')
                o = URIRef(choco_prefix + j['id'].replace(" ", "_"))
                links.add((s, p, o))
                with open(links_outfile, 'a') as linksfile:
                    linksfile.write(links.serialize(format='nt'))
                links = Graph()
                # time.sleep(1)


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: {0} <midi file path> <jams file path> <links outfile>".format(sys.argv[0]))
        exit(2)

    midi_choco_links(sys.argv[1], sys.argv[2], sys.argv[3])

    exit(0)
