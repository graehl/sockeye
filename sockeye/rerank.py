# Copyright 2017--2021 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may not
# use this file except in compliance with the License. A copy of the License
# is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is distributed on
# an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied. See the License for the specific language governing
# permissions and limitations under the License.

"""
CLI to rerank an nbest list of translations.
"""

import argparse
from functools import partial
import json
import logging
import sys
from typing import Any, Dict, List

import numpy as np
import sacrebleu

from . import arguments
from . import constants as C
from . import log
from . import utils

logger = logging.getLogger(__name__)


class Reranker:
    """
    Reranks a list of hypotheses according to a sentence-level metric.

    :param metric: Sentence-level metric such as smoothed BLEU.
    :param return_score: If True, also return the sentence-level score.
    :param isometric_alpha: Factor for reranking with isometric criteria.
    """

    def __init__(self, metric: str, isometric_alpha: float = 0.5, return_score: bool = False) -> None:
        self.metric = metric
        self.isometric_alpha = isometric_alpha
        self.return_score = return_score

        if self.metric == C.RERANK_BLEU:
            # "add-k" smoothing is the best-performing method implemented in
            # sacrebleu.  See "Method 2" results from Chen and Cherry
            # (http://aclweb.org/anthology/W14-3346)
            self.scoring_function = partial(sacrebleu.sentence_bleu, smooth_method='add-k')
        elif self.metric == C.RERANK_CHRF:
            self.scoring_function = sacrebleu.sentence_chrf  # type: ignore
        elif self.metric.startswith(C.RERANK_ISOMETRIC):
            self.scoring_function = partial(utils.compute_isometric_score, isometric_metric=self.metric,
                                            isometric_alpha=self.isometric_alpha)
        else:
            raise utils.SockeyeError("Scoring metric '%s' unknown. Choices are: %s" % (metric, C.RERANK_METRICS))

        if self.metric == C.RERANK_ISOMETRIC_LC:
            self.ranking_indices = partial(self._get_ranking_indices, kind='mergesort', order='ascending')
        else:
            self.ranking_indices = partial(self._get_ranking_indices, kind='mergesort', order='descending')


    def rerank(self, hypotheses: Dict[str, Any], reference: str) -> Dict[str, Any]:
        """
        Reranks a set of hypotheses that belong to one single reference
        translation. Uses stable sorting.

        :param hypotheses: Nbest translations.
        :param reference: A single string with the actual reference translation.
        :return: Nbest translations sorted by reranking scores.
        """
        if self.metric == C.RERANK_BLEU or self.metric == C.RERANK_CHRF:
            scores = [self.scoring_function(hypothesis, [reference]).score for
                      hypothesis in hypotheses['translations']]
            # BLEU, CHRF - the higher, the better
            ranking = self.ranking_indices(scores)

        if self.metric.startswith(C.RERANK_ISOMETRIC):
            source = hypotheses['text']
            # pylint: disable=redundant-keyword-arg
            scores = [self.scoring_function(hypothesis, hypothesis_score[0], source) for
                      hypothesis, hypothesis_score in zip(hypotheses['translations'], hypotheses['scores'])]
            # isometric-lc - the smaller, the better
            ranking = self.ranking_indices(scores)

        reranked_hypotheses = self._sort_by_ranking(hypotheses, ranking)
        if self.return_score:
            reranked_hypotheses['scores'] = [scores[i] for i in ranking]
            reranked_hypotheses['score'] = reranked_hypotheses['scores'][0]

        return reranked_hypotheses

    @staticmethod
    def _get_ranking_indices(scores: List, kind: str = 'mergesort', order: str = 'descending') -> List:
        if order == 'descending':
            return list(np.argsort(scores, kind=kind)[::-1])  # type: ignore
        else:
            return list(np.argsort(scores, kind=kind))  # type: ignore

    @staticmethod
    def _sort_by_ranking(hypotheses: Dict[str, Any], ranking: List[int]) -> Dict[str, Any]:
        def ranksort(l):
            # Sort lists in hypotheses object (translations, scores) and return
            # non-lists (sentence_id, score, translation) unchanged.
            if not isinstance(l, list):
                return l
            return [l[i] for i in ranking]

        return {key: ranksort(value) for key, value in hypotheses.items()}


def rerank(args: argparse.Namespace):
    """
    Reranks a list of hypotheses according to a sentence-level metric.
    Writes all output to STDOUT.

    :param args: Namespace object holding CLI arguments.
    """
    reranker = Reranker(args.metric, args.isometric_alpha, args.return_score)
    output_stream = sys.stdout if args.output is None else utils.smart_open(args.output, mode='w')
    logger.info("Hypotheses re-ranking using criterion: '%s' " % args.metric)

    with utils.smart_open(args.reference) as reference, utils.smart_open(args.hypotheses) as hypotheses:
        for i, (reference_line, hypothesis_line) in enumerate(zip(reference, hypotheses), 1):
            reference = reference_line.strip()
            # Expects a JSON object with keys containing at least 'translations',
            # as returned by sockeye.translate's nbest output
            hypotheses = json.loads(hypothesis_line.strip())
            utils.check_condition('translations' in hypotheses,
                                  "Reranking requires nbest JSON input with 'translations' key present.")
            num_hypotheses = len(hypotheses['translations'])

            if not num_hypotheses > 1:
                logger.info("Line %d contains %d hypotheses. Nothing to rerank.", i, num_hypotheses)
                reranked_hypotheses = hypotheses
            else:
                reranked_hypotheses = reranker.rerank(hypotheses, reference)

            if args.output_best:
                best_hypothesis = reranked_hypotheses['translations'][0] if num_hypotheses else ''

                if not best_hypothesis and args.output_reference_instead_of_blank:
                    logger.warning('Line %d: replacing blank hypothesis with reference.', i)
                    best_hypothesis = reference

                # get best non-blank hypothesis, when reference is not used
                if not best_hypothesis and args.output_best_non_blank and num_hypotheses > 1:
                    for h in range(num_hypotheses):
                        best_hypothesis = reranked_hypotheses['translations'][h]

                        if not best_hypothesis:
                            continue
                        else:
                            logger.warning('Line %d: blank hypothesis replaced by line [%d] non-blank '
                                           'hypothesis: %s .', h - 1, h, best_hypothesis)
                            break

                print(best_hypothesis, file=output_stream)
            else:
                print(json.dumps(reranked_hypotheses, sort_keys=True), file=output_stream)

    if output_stream is not sys.stdout:
        output_stream.close()


def main():
    """
    Commandline interface to rerank nbest lists.
    """
    log.setup_main_logger(console=True, file_logging=False)
    log.log_sockeye_version(logger)

    params = argparse.ArgumentParser(description="Rerank nbest lists of translations."
                                                 " Reranking sorts a list of hypotheses according"
                                                 " to their score compared to a common reference or"
                                                 "source sentence.")
    arguments.add_rerank_args(params)
    args = params.parse_args()

    logger.info(args)

    rerank(args)


if __name__ == "__main__":
    main()
