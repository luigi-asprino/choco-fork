"""
Utilities to parse iReal Pro chart data and extract chord annotations.
The implementation currently leverages `pyRealParser`.

"""
import os
import re
import glob
import copy
import urllib
import logging
import itertools

import jams
import numpy as np
import pandas as pd
from pyRealParser import Tune
from tqdm import tqdm
from joblib import Parallel, delayed, parallel_backend


from jams_utils import register_jams_meta, register_annotation_meta
from jams_score import append_listed_annotation, to_jams_timesignature
from ireal_db import iRealDatabaseHandler
from utils import create_dir, pad_substring

logger = logging.getLogger("choco.ireal_parser")

IREAL_RE = r'irealb://([^"]+)'
IREAL_NREP_RE = r"{.+?N\d.+?}"  # to identify all complex repeats
IREAL_REPEND_RE = r"([{\[|]?)\s*N(\d)"  # to identify all ending markers
IREAL_CHORD_RE = r'(?<!/)([A-Gn][^A-G/]*(?:/[A-G][#b]?)?)'  # an iReal chord


def split_ireal_charts(ireal_url:str):
    """
    Read an iReal URL wrapping custom-encoded chord annotations and split them
    in separate (piece-specific) annotations for further processing.

    Parameters
    ----------
    ireal_url : str
        An iReal Pro raw URL wrapping one or more (playlist) chord annotations.

    Returns
    -------
    charts : list
        A list of iReal charts, with one element per chart.

    """
    ireal_string = urllib.parse.unquote(ireal_url)
    match = re.match(IREAL_RE, ireal_string)
    if match is None:
        raise RuntimeError('Provided string is not a valid iReal url!')
    # Split the url into individual charts along the '===' separator
    charts = re.split("===", match.group(1))
    charts = [c for c in charts if c != '']

    return charts


def mjoin(chord_string:str, *others):
    """
    Metrical join between chord strings, inserting a measure symbol "|" among 
    consecutive chord strings only if a (bar) separator is missing. Everything
    will look like this: "string_a | string_b | ... | string_z".
    """
    pre_chords = [cs for cs in [chord_string]+list(others) if cs.strip() != ""]
    merged_chords = [pre_chords[0]]  # take first non-empty string
    for next_cstring in pre_chords[1:]:
        # print(next_cstring + "\n\n")
        separator = "|" if merged_chords[-1].rstrip()[-1] != "|" \
            and next_cstring.lstrip()[0] != "|" else ""
        merged_chords.append(separator+next_cstring)

    merged_chords = "".join(merged_chords)
    return merged_chords


class ChoCoTune(Tune):

    @classmethod
    def _insert_missing_repeat_brackets(cls, chord_string):
        """
        Handle implicit curly brackets and insert one at the beginning of the
        chord string, where the repetition is trivially expected to start.
        """
        if chord_string.count("{") != chord_string.count("}"):
            logger.warning("Uneven number of curly brackets in chord string")
        # Evening out repeating bar lines for consistent expansion
        cbracket_opn_loc = chord_string.find("{")
        cbracket_cld_loc = chord_string.find("}")

        if cbracket_cld_loc > -1 and (cbracket_opn_loc == -1 \
            or cbracket_opn_loc > cbracket_cld_loc):
            # Assume that there is a leading open bracket missing
            logger.warning("Inserting leading '{' to compensate")
            chord_string = '{' + chord_string

        return chord_string

    @classmethod
    def _fill_long_repeats(cls, chord_string):
        """
        Replaces long repeats with multiple endings with the appropriate chords.

        Parameters
        ----------
        chord_string : str
            The chord string following cleanup, insertion of missing brackets,
            and (optionally but preferably) removal of extra comments besides
            those wrapping explicit rounds of repetition.
        
        Returns
        -------
        new_chord_string : str
            A new chord string resulting from the expansion of repetitions of
            two kinds: complex repetitions with different endings (marked by N1,
            N2, etc. symbols), and full bracketed repetitions where an optional
            special comment may be used to indicate the number of repeats.

        """
        repeat_match = re.search(r'{(.+?)}', chord_string)
        if repeat_match is None:
            return chord_string
        full_repeat = repeat_match.group(1)

        # Check whether there is a first ending in the repeat
        number_match = re.search(r'N(\d)', full_repeat)
        if number_match is not None:
            # Sanity check and verification of additional numbered repeats
            macro_repeats = list(re.finditer(IREAL_NREP_RE, chord_string))
            logger.info(f"Found {len(macro_repeats)} complex repeat(s)")
            current_mrstart = macro_repeats[0].start()  # of this macro repeat
            current_bnd = macro_repeats[1].start() \
                if len(macro_repeats) > 1 else len(chord_string)
            assert current_mrstart < current_bnd, \
                f"Illegal substitution: current macro repeat starts at " \
                f"{current_mrstart}, next macro (or end) at {current_bnd}"
            # Now, get rid of the first repeat number and the curly braces
            first_repeat = re.sub(r'N\d', '', full_repeat)
            logger.info(f"Resolving first marked repeat in {full_repeat}")
            new_chord_string = mjoin(
                chord_string[:repeat_match.start()], \
                first_repeat, chord_string[repeat_match.end():])
            # Remove the first repeat ending as well as segnos and codas
            repeat = cls._remove_markers(
                re.search(r'([^N]+)N\d', full_repeat).group(1))
            # Find the next ending markers and insert the repeated chords before
            for _ in re.findall(IREAL_REPEND_RE, new_chord_string):
                mrep = lambda x: x.group(1) + repeat \
                        if x.start() < current_bnd else x.group(0)
                new_chord_string = re.sub(
                    IREAL_REPEND_RE, mrep, new_chord_string)

        else:  # Bracket repeat: which can either be performed twice or more
            to_repeat, times = full_repeat, 2  # defaul case (brackets only)
            # Check whether the number of repeats is explicitly annotated
            no_repeats_match = list(re.finditer(r'<(\d+)x>', full_repeat))
            if len(no_repeats_match) > 0:  # explicit repeats provided
                times = int(no_repeats_match[-1].group(1))  # use last marker
                s, e = no_repeats_match[-1].start(), no_repeats_match[-1].end()
                to_repeat = full_repeat[:s] + full_repeat[e:]
                to_repeat = to_repeat.replace("  ", " ")
            # Unroll th repetition but keep the first occurrence with markers
            logger.info(f"Times {times} repeat of {to_repeat}")
            repetitions = (' |' + cls._remove_markers(to_repeat)) * (times-1)
            new_chord_string = mjoin(
                chord_string[:repeat_match.start()], \
                to_repeat, repetitions, \
                chord_string[repeat_match.end():])  # + '|'

        # There could be other repeat somewhere, so we need to go recursive
        new_chord_string = cls._fill_long_repeats(new_chord_string)
        return new_chord_string
    
    @classmethod
    def _fill_codas(cls, chord_string):
        """
        Flatten 'D.C. al Coda' and 'D.S. al Coda' by repeating from the head of 
        the chord string, or the first segno (if present), to the first 'Q'
        and then a jump to the second 'Q'.
    
        Parameters
        ----------
        chord_string : str
            The chord string following the extension of long repeats, but still
            containing the coda markers (Q) and segnos (S).

        Returns
        -------
        chord_string : str
            The chord string with filled 'D.C. al Coda' and 'D.S. al Coda'

        """
        qs = chord_string.count('Q')
        if qs > 2:  # unformatted chart or unsupported XXX
            raise RuntimeError(f"Could not parse codas: " + \
                "number of Qs expected to be 0, 1 or 2, not {qs}!")

        elif qs == 1:  # coda is used to indicate an outro: just get rid of it
            chord_string = chord_string.replace('Q', '')

        elif qs == 2:  # repeat from the head/S to first Q then jump to second Q
            q1, q2 = [i for i, c in enumerate(chord_string) if c == 'Q']
            segno = chord_string.find('S')  # get the first segno
            segno = 0 if segno == -1 else segno + 1  # segno as offset
            # Ready to extract coda and repeat, then infill
            coda = chord_string[q2 + 1:]
            repeat = chord_string[segno:q1]
            new_chord_string = chord_string[:q2] + repeat + ' |' + coda
            new_chord_string = re.sub(r'[QS]', '', new_chord_string)
            return new_chord_string

        return chord_string

    @classmethod
    def _fill_single_double_repeats(cls, measures):
        """
        Replaces 1- and 2-measure repeat symbols with the appropriate chords:
        'x' repeats the previous measure, 'r' repeates the former two. 

        Parameters
        ----------
        measures : list of str
            A list of measures, eacnh encoded as a string.

        Returns
        -------
        new_measures : list of str
            A new list of measures where 'x' and 'r' are infilled.

        """
        pre_measures = []
        for measure in measures:  # marker has its own parenthesis
            splits = re.split(r"[rx]", measure)
            markers = re.findall(r"[rx]", measure)
            measures_xt = [val.strip() \
                for pair in zip(splits, markers+[""]) \
                for val in pair if val.strip() != ""]
            pre_measures += measures_xt

        new_measures = []
        # Add extra measures for double repeats
        for i, measure in enumerate(pre_measures):
            if measure == 'r':  # just prepare the ground for later
                new_measures.append(measure)
                if i+1 == len(pre_measures) or pre_measures[i+1].strip() != "":
                    new_measures.append(" ")  # empty slot needed for r
            else:  # anything else is kept as it is
                new_measures.append(measure)

        # 1- and 2-measure repeats: safe now
        for i in range(1, len(new_measures)):
            if new_measures[i] == 'x':  # infill the 1-m repeat from last bar
                new_measures[i] = cls._remove_markers(new_measures[i-1])
            elif new_measures[i] == 'r':  # infill the 2-m repeat from last two
                new_measures[i] = cls._remove_markers(new_measures[i-2])
                assert new_measures[i+1].strip() == "", \
                    f"Illegal repeat: non-empty bar ahead: {new_measures[i+1]}"
                new_measures[i+1] = cls._remove_markers(new_measures[i-1])

        return new_measures

    @classmethod
    def _fill_slashes(cls, measures):
        """
        Replace slash symbols (encoded as 'p') with the previous chord.

        Parameters
        ----------
        measures : list of str
            A list of chord measures (strings) where single and double repeats
            ('x' and 'r') have already been infilled in previous stages.

        Returns
        -------
        new_measures : list of str
            A new list of measures with filled slashes.

        """
        chord_regex = re.compile(IREAL_CHORD_RE)

        for i in range(len(measures)):
            while measures[i].find('p') != -1:
                slash = measures[i].find('p')
                if slash == 0:  # slash in 1st position needs measure lookback
                    prev_chord = chord_regex.findall(measures[i - 1])[-1] + " "
                    measures[i] = prev_chord + measures[i][1:]
                    measures[i] = re.sub(r'^(p+)', prev_chord, measures[i])  # ?
                else:  # repeating a chord that should be found in the same bar
                    prev_chord = chord_regex.findall(measures[i][:slash])[-1]
                    measures[i] = measures[i][:slash] + \
                        prev_chord + " " + measures[i][slash + 1:]

        return measures

    @classmethod
    def _clean_measures(cls, measures):
        """
        Post-processing chord symbols to make sure iReal-specific markers end up
        separated from the chordal information. This includes, no-chord symbols
        (N.C. encoded as concatenated 'n' markers), extra spaces, end and segno
        symbols, as well as the infill of ovals (e.g. W/C).

        Parameters
        ----------
        measures : list of str
            A list of measures at the final stage of pre-processing.

        Returns
        -------
        new_measures : list of str
            A new list of measures where all chords are individually readable.

        Notes
        -----
            - The time complexity of this function can be fairly improved, as it
                currently involves several passes through the measure sequence;
                however, this is still somehow desirable, and neglegible, to
                avoid mixing steps that are conceptually differnent.
            - Filling ovals could deserve to be in a separate function before
                the current is actually applied; it is not cosmetics, indeed.

        """
        # No-chord symbols are padded and capitalised
        new_measures = copy.deepcopy(measures)
        for i in range(len(new_measures)):
            while new_measures[i].find('n') != -1:  # safe with replace
                new_measures[i] = pad_substring(new_measures[i], "n", "N")

        for i in range(len(new_measures)):  # filling ovals with previous root
            for observation in new_measures[i].split():
                chord_match = re.match(IREAL_CHORD_RE, observation)
                if chord_match:  # record last chord to be ready to infill root
                    last_root = chord_match.group(1).split("/")[0]
                else:  # this can be any other element, or possibly a W/?
                    if "W" in observation:  # fire a single replace (count 1)
                        new_measures[i] = re.sub(
                            r"W", last_root, new_measures[i], count=1)

        for i in range(len(new_measures)):  # final pass to remove extras
            measure_tmp = new_measures[i]
            measure_tmp = re.sub(r"[US]", "", measure_tmp)
            measure_tmp = re.sub(r"\s\s+", " ", measure_tmp).strip()
            # Now some rare cases of non-handled repeats due to bad formatting
            if re.search("N\d", measure_tmp):
                bad_repeat = re.search(r"N\d", measure_tmp).group(0)
                logger.warning(f"Removing unhandled repeat: {bad_repeat}")
                measure_tmp = re.sub(r"N\d", "", measure_tmp)
            new_measures[i] = measure_tmp

        return new_measures

    @classmethod
    def _cleanup_chord_string(cls, chord_string):
        """
        Removes excessive whitespace, unnecessary stuff, empty measures etc.
        and return a more readable string.

        Parameters
        ----------
        chord_string: str
            Unscrambled chords in string form
        
        Returns
        -------
        chord_string: str
            The same string following the preliminary cleaning step.

        """
        # Unify symbol for new measure to |
        chord_string = re.sub(r'LZ|K', '|', chord_string)
        # Unify symbol for one-bar repeat to x
        chord_string = re.sub(r'cl', 'x', chord_string)
        # Remove stars with empty space in between
        chord_string = re.sub(r'\*\s*\*', '', chord_string)
        # Remove vertical spacers
        chord_string = re.sub(r'Y+', '', chord_string)
        # Remove empty space
        chord_string = re.sub(r'XyQ|,', ' ', chord_string)
        # Remove empty measures
        chord_string = re.sub(r'\|\s*\|', '|', chord_string)
        # Remove end markers
        chord_string = re.sub(r'Z', '', chord_string)

        # Padding of nested chord annotations: case of ovals
        chord_string = pad_substring(
            chord_string, r"W(?:/[A-G][#b]?)?", recursive=True)

        # remove spaces behing bar lines
        chord_string = re.sub(r'\|\s+', '|', chord_string)       
        # remove multiple white-spaces
        chord_string = re.sub(r'\s+', ' ', chord_string)
        # remove trailing white-space
        chord_string = chord_string.rstrip()

        return chord_string

    @classmethod
    def _remove_unsupported_annotations(cls, chord_string):
        """
        Removes certain annotations that are currently not handled/used by the
        parser, including section markers, alternative chords, time signatures,
        as well as those providing little or none musical content.

        Notes:
            - In some cases, annotations are concatenated with chords, or other
                annotations; in these cases, it is better to replace the string
                with a single space rather than a blank/nil one.

        """
        # Unify symbol for new measure to |
        chord_string = re.sub(r'[\[\]]', '|', chord_string)
        # Remove empty measures: safe because of "n" and "p"
        chord_string = re.sub(r'\|\s*\|', '|', chord_string)
        # Remove comments except explicit repeat markers (<3x>)
        chord_string = re.sub(r'(?!<\d+x>)<.*?>', '', chord_string)
        # Remove alternative chords, but keep space in-betweens
        chord_string = re.sub(r'\([^)]*\)', ' ', chord_string)
        # remove unneeded single l and f (fermata)
        chord_string = re.sub(r'[lf]', '', chord_string)
        # Remove s (for 'small), unless it's part of a sus chord
        chord_string = re.sub(r'(?<!su)s(?!us)', '', chord_string)
        # Remove section markers
        chord_string = re.sub(r'\*\w', '', chord_string)
        # Remove time signatures
        chord_string = re.sub(r'T\d+', '', chord_string)

        return chord_string

    @classmethod
    def _get_measures(cls, chord_string):
        """
        Split a chord string into a list of measures, where empty measures are
        discarded. Cleans up the chord string, removes annotations, and handles
        repeats & codas as well.

        Parameters
        ----------
        chord_string: str
            A chord string as originally encoded according to iReal's URL.

        Returns
        -------
        measures : list
            A list of measures, with the contents of every measure as a string.

        """
        chord_string = cls._cleanup_chord_string(chord_string)
        chord_string = cls._insert_missing_repeat_brackets(chord_string)
        # Time to remove unsupported annotations, and improve consistency
        chord_string = cls._remove_unsupported_annotations(chord_string)
        # Unrolling repeats with different endings and coda-based
        chord_string = cls._fill_long_repeats(chord_string)
        chord_string = cls._fill_codas(chord_string)
        # Separating chordal content based on bar markers
        measures = re.split(r'\||LZ|K|Z|{|}|\[|\]', chord_string)
        measures = [m.strip() for i, m in enumerate(measures) \
            if m.strip() != '' or measures[i-1].strip() == "r"]  # XXX n.n.
        measures = [m.replace("U", "").strip() for m in measures]
        # Infill measure repeat markers (x, r) and within-measure (p)
        measures = cls._fill_single_double_repeats(measures)
        measures = cls._fill_slashes(measures)
        measures = cls._clean_measures(measures)

        return measures

    @staticmethod
    def parse_ireal_url(url):
        """
        Parse iReal charts (URL) into human- and machine-readable formats.

        Parameters
        ----------
        url : str
            An url-like string containing one or more tunes.

        Returns
        -------
        tunes : list
            A list of ChoCoTune objects resulting from the parsing.
        pname : str
            The name of the playlist if the given charts are bundled.

        """
        charts = split_ireal_charts(url)

        tunes, pname = [], None
        for i, chart in enumerate(charts):
            if i == len(charts)-1 and "=" not in chart:
                pname = chart.strip(); break  # fermete
            try:  # attempt parsing of the individual tune
                tune = ChoCoTune(chart)
                tunes.append(tune)
                logger.info(f"Parsed tune {i}: {tune.title}")
            except Exception as err:
                logger.warn(f"Cannot import tune {i}: {err}")

        return tunes, pname


def extract_metadata_from_tune(tune: ChoCoTune, tune_id=None):
    """
    Extract metadata information from an iReal tune, an object resulting from
    parsing an original chart with the `parse_ireal_url` function.

    Parameters
    ----------
    tune : ChoCoTune
        The ChoCoTune instance from which metadata needs to be extracted.
    tune_id : str
        An optional string that can be used for indexing the tune in the dict.
    
    Returns
    -------
    metadata : dict
        A dictionary providing the metadata extracted from the given tune.

    """
    metadata = {
        "id": tune_id,
        "title": tune.title,
        "artists": tune.composer,
        "genre": tune.style,
        "tempo": tune.bpm,
        "time_signature": tune.time_signature,
    }

    return metadata


def extract_annotations_from_tune(tune: ChoCoTune):
    """
    Extract chord and key annotations from a given tune.

    Parameters
    ----------
    tune : ChoCoTune
        The ChoCoTune instance from which music annotations will be extracted.

    Returns
    -------
    chords : list of lists
        A list of chord annotations as tuples: (measure, beat, duration, chord) 
    keys : list of lists
        A list of chord annotations as tuples: (measure, beat, duration, key)

    Notes
    -----
    - Still need to retrieve chord-specific duration from the tune.
    - Durations are not consistent when there are time-signature changes.
    - Could include more annotations rather than just chords.

    """
    measures = tune.measures_as_strings
    measure_beats = tune.time_signature[0]
    beat_duration = measure_beats*len(measures)

    chords = []  # iterating and timing chords
    for m, measure in enumerate(measures, 1):
        measure_chords = measure.split()
        chord_dur = measure_beats / len(measure_chords)
        # Creating equal onsets depending on within-measure chords and beats
        onsets = np.cumsum([0]+[d for d in (len(measure_chords)-1)*[chord_dur]])
        chords += [[m, o, chord_dur, c] for o, c in zip(onsets, measure_chords)]
    # Encapsulating key information as a single annotation
    assert len(tune.key.split()) == 1, "Single key assumed for iReal tunes"
    keys = [[1, 1, beat_duration, tune.key]]
    time_signatures = f"{tune.time_signature[0]}/{tune.time_signature[1]}"
    time_signatures = [[1, 1, beat_duration, time_signatures]]
    return chords, keys, time_signatures


def jamify_ireal_tune(tune:ChoCoTune):
    """
    Create a JAMS from a given iReal tune, provided as ChoCoTune object, and
    a dictionary containing the metadata extracted from the tune.

    Parameters
    ----------
    tune : ChocoTune
        An instance of ChoCoTune, which will be jamified.

    Returns
    -------
    tune_meta : dict
        A dictionary containing the metadata of the tune.
    jam : jams.JAMS
        A JAMS object wrapping the tune annotations.

    """
    jam = jams.JAMS()
    tune_meta = extract_metadata_from_tune(tune)
    chords, keys, time_signatures = extract_annotations_from_tune(tune)

    register_jams_meta(
        jam, jam_type="score",
        expanded=True,
        title=tune_meta["title"],
        artist=tune_meta["artists"],
        duration=chords[-1][0]+1,
        genre=tune_meta["genre"],
    )
    jam.sandbox["tempo"] = tune_meta["tempo"]  # XXX should be annotation
    append_listed_annotation(jam, "chord_ireal", chords, offset_type="beat")
    append_listed_annotation(jam, "key_mode", keys, offset_type="beat")
    append_listed_annotation(jam, "timesig", time_signatures,
                             offset_type="beat", value_fn=to_jams_timesignature)

    return tune_meta, jam


def process_ireal_string(chart_string:str):
    """
    Read an iReal chart string and generate a JAMS annotation with metadata.

    Parameters
    ----------
    chart_string : str
        A string containing a single iReal chart after decoding and splitting.

    Returns
    -------
    metadata : dict
        A dictionary containing the metadata extracted from the tune.
    jams_list : jams.JAMS
        A JAMS file with chord/key annotations extracted from the tune.

    """
    tune = ChoCoTune(chart_string)
    metadata, jam = jamify_ireal_tune(tune)

    return metadata, jam


def process_ireal_charts(chart_data):
    """
    Read and process iReal chart data or tunes to create a JAMS dataset.

    Parameters
    ----------
    chart_data : list of ChoCoTune, or str
        Either a list containing instances of ChoCoTune created previously, or
        a string encoding all (raw) charts, or a path to a file containing the
        raw iReal charts.

    Returns
    -------
    metadata_list : list of dicts
        A list of dictionaries, each providing the metadata of a single tune.
    jams_list : list of jams.JAMS
        A list of JAMS files generated from the extraction process.

    """
    if isinstance(chart_data, str):
        if os.path.isfile(chart_data):
            with open(chart_data, 'r') as charts:
                chart_data = charts.read()
        if re.match(IREAL_RE, chart_data):
            tunes, _ = ChoCoTune.parse_ireal_url(chart_data)
    elif isinstance(chart_data, list) and \
        isinstance(chart_data[0], ChoCoTune):
        tunes = chart_data  # ready to go

    else:  # none of the supported parameter types/formats
        raise ValueError("Not a valid supported format or broken charts")
    
    jam_pack = [jamify_ireal_tune(tune) for tune in tunes]
    metadata_list, jam_list = list(zip(*jam_pack))

    return metadata_list, jam_list


def parse_ireal_dataset(dataset_dir, out_dir, dataset_name, **kwargs):
    """
    Process an iReal dataset to extract metadata information as well as JAMS
    annotations of chords and keys.

    Parameters
    ----------
    dataset_dir : str
        Path to an iReal dataset containing chart data in .txt files.
    out_dir : str
        Path to the output directory where JAMS annotations will be saved.
    dataset_name : str
        Name of the dataset that which will be used for the creation of new ids
        in both the metadata returned the JAMS files produced.

    Returns
    -------
    metadata_df : pandas.DataFrame
        A dataframe containing the retrieved and integrated content metadata.

    """
    offset_cnt = 0
    all_metadata = []

    jams_dir = create_dir(os.path.join(out_dir, "jams"))
    chart_files = glob.glob(os.path.join(dataset_dir, "*.txt"))
    logger.info(f"Found {len(chart_files)} .txt files for iReal parsing")

    for chart_file in chart_files:
        for i, (meta, jam) in enumerate(zip(*process_ireal_charts(chart_file))):

            meta["id"] = f"{dataset_name}_{offset_cnt + i}"
            meta["jams_path"] = None  # in case of error
            # Annotation metadata in the JAMS file
            register_annotation_meta(jam,
                annotator_type="crowdsource",
                annotation_version=kwargs.get("dataset_version", 1.0),
                annotation_tools="https://www.irealpro.com",
                dataset_name="iReal Pro",
            )
            jams_path = os.path.join(jams_dir, meta["id"]+".jams")
            try:  # attempt saving the JAMS annotation file to disk
                jam.save(jams_path, strict=False)
                meta["jams_path"] = jams_path
            except Exception as e:  # dumping error, logging for now
                logging.error(f"Could not save: {jams_path}: {e}")
            all_metadata.append(meta)
        offset_cnt = offset_cnt + i + 1
    # Finalise the metadata dataframe
    metadata_df = pd.DataFrame(all_metadata)
    metadata_df = metadata_df.set_index("id", drop=True)
    metadata_df.to_csv(os.path.join(out_dir, "meta.csv"))

    return metadata_df


def parse_ireal_forum_thread(thread_charts, jams_dir, dataset_name, ireal_db):
    """
    Process a list of iReal charts that were extracted from a specific thread in
    the forum, and extract unique chord annotations and content metadata that
    are not already present in ChoCo.

    Parameters
    ----------
    thread_charts : str
        Path to a CSV file containing a list of charts found in the thread.
    jams_dir : str
        Path to the output directory where JAMS annotations will be saved.
    dataset_name : str
        Name of the dataset that which will be used for the creation of new ids
        in both the metadata returned the JAMS files produced.
    ireal_db : ireal_db.iRealDatabaseHandler
        Handle to the iReal database, need to register charts and get IDs.

    Returns
    -------
    metadata : list of dicts
        A list tune-specific dictionaries containing extracted metadata.

    """
    all_metadata = []
    thread_tunes = pd.read_csv(thread_charts)

    thread_name = os.path.splitext(os.path.basename(thread_charts))[0]
    mainpage_name = os.path.basename(os.path.dirname(thread_charts))
    logger.info(f"Thread '{thread_name}' ({mainpage_name}): "
                f"{len(thread_tunes)} charts")

    for _, charts in thread_tunes.iterrows():

        try:  # attempt to read and split and decode a charts string
            charts_splitted = split_ireal_charts(charts["ireal_charts"])
        except Exception as err:  # dumping error, logging for now
            logger.error(f"Cannot split/decode raw charts: {err}")
            continue  # just ignore and go to next chart

        charts_name = charts['name']  # name of single tune or playlist
        logger.info(f"Chart {charts_name} has {len(charts_splitted)} tunes")

        for i, chart in enumerate([c for c in charts_splitted if "=" in c]):
            id_number = ireal_db.register_chart(chart)
            if id_number is None:  # check repeated entry
                logger.warning(f"Chart '{charts_name}/{i}' already in iReal DB")
                continue  # just ignore and go to next tune

            try:  # read, parse and process the ireal chart if possible
                meta, jam = process_ireal_string(chart)
                ireal_db.register_metadata(id_number, meta)
            except Exception as err:  # dumping error, logging for now
                logger.error(f"Cannot parse {id_number}: {err}")
                continue  # just ignore and go to next tune

            meta["id"] = f"{dataset_name}_{id_number}"
            meta["jams_path"] = None  # null-default path before saving
            jams_path = os.path.join(jams_dir, f"{meta['id']}.jams")
            try:  # attempt saving the JAMS annotation file to disk
                jam.save(jams_path, strict=False)
                meta["jams_path"] = jams_path
                ireal_db.register_jams(id_number, jams_path)
            except Exception as err:  # dumping error, logging for now
                logger.error(f"Could not save {id_number}: {err}")

            all_metadata.append(meta)

    return all_metadata


def parse_ireal_dump(dataset_dir, out_dir, dataset_name, chocodb_path,
    n_workers=1, **kwargs):
    """
    Creates a JAMS dataset with content metadata from a dump of the iReal forum.

    Parameters
    ----------
    dataset_dir : str
        Path to the folder containing a dump of the iReal forum.
    out_dir : str
        Path to the output directory where JAMS annotations will be saved.
    dataset_name : str
        Name of the dataset that which will be used for the creation of new ids
        in both the metadata returned the JAMS files produced.
    chocodb_path : str
        Path to the ChoCo database from which new IDs are minted/retrieved.

    Returns
    -------
    metadata_df : pandas.DataFrame
        A dataframe containing the retrieved and integrated content metadata.

    """
    iRealDataset = iRealDatabaseHandler(database_path=chocodb_path)

    jams_dir = create_dir(os.path.join(out_dir, "jams"))
    forum_threads = [os.path.join(root, f) for root, _, fnames \
        in os.walk(dataset_dir) for f in fnames if f.endswith(".csv")]
    logger.info(f"Found {len(forum_threads)} threads in {dataset_dir}")

    with parallel_backend('threading', n_jobs=n_workers):
        # Spread the computation but keep everything in threads
        all_metadata = Parallel(n_jobs=n_workers)\
            (delayed(parse_ireal_forum_thread)\
                (thread_charts, jams_dir, dataset_name, iRealDataset) \
                    for thread_charts in tqdm(forum_threads))

    iRealDataset.close()
    # Finalise the metadata dataframe after merging thread-specific lists
    all_metadata = list(itertools.chain.from_iterable(all_metadata))
    metadata_df = pd.DataFrame(all_metadata) 
    metadata_df = metadata_df.set_index("id", drop=True)
    metadata_df.to_csv(os.path.join(out_dir, "meta.csv"))

    return metadata_df
