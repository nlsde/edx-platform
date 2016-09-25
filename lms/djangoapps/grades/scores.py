"""
Functionality for problem scores.
"""
from logging import getLogger

from openedx.core.lib.cache_utils import memoized
from xblock.core import XBlock
from xmodule.block_metadata_utils import display_name_with_default_escaped
from xmodule.graders import ProblemScore
from .transformer import GradesTransformer


log = getLogger(__name__)


def possibly_scored(usage_key):
    """
    Returns whether the given block could impact grading (i.e.
    has_score or has_children).
    """
    return usage_key.block_type in _block_types_possibly_scored()


def get_score(submissions_scores, csm_scores, persisted_block, block):
    """
    Returns the score for a problem, as a ProblemScore object.  It is
    assumed that the provided storages have already been filtered for
    a single user in question and have user-specific values.

    The score is retrieved from the provided storages in the following
    order of precedence.  If no value for the block is found in a
    given storage, the next storage is checked.

    submissions_scores (dict of {unicode(usage_key): (earned, possible)}):

        A python dictionary of serialized UsageKeys to (earned, possible)
        tuples. These values, retrieved using the Submissions API by the
        caller (already filtered for the user and course), take precedence
        above all other score storages.

        When the score is found in this storage, it implies the user's score
        for the block was persisted via the submissions API. Typically, this API
        is used by ORA.

        The returned score includes valid values for:
            w_earned
            w_possible
            graded - retrieved from the persisted block, if found, else from
                the latest block content.

        Note: r_earned and r_possible are not required when submitting scores
        via the submissions API.  And so those along with weight (since unused)
        are invalid and irrelevant.

    csm_scores (ScoresClient):

        The ScoresClient object (already filtered for the user and course),
        from which a courseware.models.StudentModule object can be retrieved for
        the block.

        When the score is found from this storage, it implies the user's score
        for the block was persisted in the Courseware Student Module. Typically,
        this storage is used for all CAPA problems, including scores calculated
        by external graders.

        The returned score includes valid values for:
            r_earned, r_possible - retrieved from CSM
            w_earned, w_possible - calculated from the raw scores and weight
            weight, graded - retrieved from the persisted block, if found,
                else from the latest block content

    persisted_block (.models.BlockRecord):
        The block values as found in the grades persistence layer. These values
        are used only if not found from an earlier storage, and take precedence
        over values stored within the latest content-version of the block.

        When the score is found from this storage, it implies the user has not
        yet attempted this problem, but the user's grade _was_ persisted.

        The returned score includes valid values for:
            r_earned - will equal 0.0 since the user's score was not found from
                earlier storages
            r_possible - retrieved from the persisted block
            w_earned, w_possible - calculated from the raw scores and weight
            weight, graded - retrieved from the persisted block

    block (block_structure.BlockData):
        Values from the latest content-version of the block are used only if
        they were not available from a prior storage.

        When the score is found from this storage, it implies the user has not
        yet attempted this problem and the user's grade was _not_ yet persisted.

        The returned score includes valid values for:
            r_earned - will equal 0.0 since the user's score was not found from
                earlier storages
            r_possible - retrieved from the latest block content
            w_earned, w_possible - calculated from the raw scores and weight
            weight, graded - retrieved from the latest block content
    """
    weight = _get_weight_from_block(persisted_block, block)

    # Priority order for retrieving the scores:
    # submissions API -> CSM -> grades persisted block -> latest block content
    r_earned, r_possible, w_earned, w_possible = (
        _get_score_from_submissions(submissions_scores, block) or
        _get_score_from_csm(csm_scores, block, weight) or
        _get_score_from_block(persisted_block, block, weight)
    )

    assert w_possible is not None
    has_valid_denominator = w_possible > 0.0
    graded = _get_graded_from_block(persisted_block, block) if has_valid_denominator else False

    return ProblemScore(
        r_earned,
        r_possible,
        w_earned,
        w_possible,
        weight,
        graded,
        display_name=display_name_with_default_escaped(block),
        module_id=block.location,
    )


def _get_score_from_submissions(submissions_scores, block):
    """
    Returns the score values from the submissions API if found.
    """
    if submissions_scores:
        submission_value = submissions_scores.get(unicode(block.location))
        if submission_value:
            w_earned, w_possible = submission_value
            assert w_earned >= 0.0 and w_possible > 0.0  # per contract from submissions API
            return (None, None) + (w_earned, w_possible)


def _get_score_from_csm(csm_scores, block, weight):
    """
    Returns the score values from the courseware student module, via
    ScoresClient, if found.
    """
    # If an entry exists and has r_possible (total) associated with it, we trust
    # that value. This is important for cases where a student might have seen an
    # older version of the problem -- they're still graded on what was possible
    # when they tried the problem, not what it's worth now.
    #
    # Note: Storing r_possible in CSM predates the implementation of the grades
    # own persistence layer. Hence, we have duplicate storage locations for
    # r_possible, with potentially conflicting values, when a problem is
    # attempted. Even though the CSM persistence for this value is now
    # superfluous, for backward compatibility, we continue to use its value for
    # r_possible, giving it precedence over the one in the grades data model.
    score = csm_scores.get(block.location)
    has_valid_score = score and score.total is not None
    if has_valid_score:
        r_earned = score.correct if score.correct is not None else 0.0
        r_possible = score.total
        return (r_earned, r_possible) + _weighted_score(r_earned, r_possible, weight)


def _get_score_from_block(persisted_block, block, weight):
    """
    Returns the score values, now assuming the earned score is 0.0 - since a
    score was not found in an earlier storage.
    Uses the r_possible value from the persisted_block if found, else from
    the latest block content.
    """
    r_earned = 0.0

    if persisted_block:
        r_possible = persisted_block.r_possible
    else:
        r_possible = block.transformer_data[GradesTransformer].max_score

    return (r_earned, r_possible) + _weighted_score(r_earned, r_possible, weight)


def _get_weight_from_block(persisted_block, block):
    """
    Returns the weighted value from the persisted_block if found, else from
    the latest block content.
    """
    if persisted_block:
        return persisted_block.weight
    else:
        return getattr(block, 'weight', None)


def _get_graded_from_block(persisted_block, block):
    """
    Returns the graded value from the persisted_block if found, else from
    the latest block content.
    """
    if persisted_block:
        return persisted_block.graded
    else:
        return _get_explicit_graded(block)


def _get_explicit_graded(block):
    """
    Returns the explicit graded field value for the given block.
    """
    field_value = getattr(
        block.transformer_data[GradesTransformer],
        GradesTransformer.EXPLICIT_GRADED_FIELD_NAME,
        None,
    )

    # Set to True if grading is not explicitly disabled for
    # this block.  This allows us to include the block's score
    # in the aggregated self.graded_total, regardless of the
    # inherited graded value from the subsection. (TNL-5560)
    return True if field_value is None else field_value


def _weighted_score(r_earned, r_possible, weight):
    """
    Returns a tuple that represents the weighted (earned, possible) score.
    If weight is None or r_possible is 0, returns the original values.
    """
    assert r_possible is not None
    cannot_compute_with_weight = weight is None or r_possible == 0
    if cannot_compute_with_weight:
        return r_earned, r_possible
    else:
        return float(r_earned) * weight / r_possible, float(weight)


@memoized
def _block_types_possibly_scored():
    """
    Returns the block types that could have a score.

    Something might be a scored item if it is capable of storing a score
    (has_score=True). We also have to include anything that can have children,
    since those children might have scores. We can avoid things like Videos,
    which have state but cannot ever impact someone's grade.
    """
    return frozenset(
        cat for (cat, xblock_class) in XBlock.load_classes() if (
            getattr(xblock_class, 'has_score', False) or getattr(xblock_class, 'has_children', False)
        )
    )
