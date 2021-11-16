# -*- coding: cp936 -*-
# -*- coding: utf-8 -*-

import gc
import glob
import hashlib
import itertools
import json
import os
import re
import subprocess
import time
from os.path import join as pjoin

import torch

from multiprocessing import Pool
from pytorch_pretrained_bert import BertTokenizer

from others.logging import logger
from prepro.utils import _get_word_ngrams
import emoji


def cal_rouge(evaluated_ngrams, reference_ngrams):
    reference_count = len(reference_ngrams)
    evaluated_count = len(evaluated_ngrams)

    overlapping_ngrams = evaluated_ngrams.intersection(reference_ngrams)
    overlapping_count = len(overlapping_ngrams)

    if evaluated_count == 0:
        precision = 0.0
    else:
        precision = overlapping_count / evaluated_count

    if reference_count == 0:
        recall = 0.0
    else:
        recall = overlapping_count / reference_count

    f1_score = 2.0 * ((precision * recall) / (precision + recall + 1e-8))
    return {"f": f1_score, "p": precision, "r": recall}


def combination_selection(doc_sent_list, abstract_sent_list, summary_size):
    def _rouge_clean(s):
        return re.sub(r'[^a-zA-Z0-9 ]', '', s)

    max_rouge = 0.0
    max_idx = (0, 0)
    abstract = sum(abstract_sent_list, [])
    abstract = _rouge_clean(' '.join(abstract)).split()
    sents = [_rouge_clean(' '.join(s)).split() for s in doc_sent_list]
    evaluated_1grams = [_get_word_ngrams(1, [sent]) for sent in sents]
    reference_1grams = _get_word_ngrams(1, [abstract])
    evaluated_2grams = [_get_word_ngrams(2, [sent]) for sent in sents]
    reference_2grams = _get_word_ngrams(2, [abstract])

    impossible_sents = []
    for s in range(summary_size + 1):
        combinations = itertools.combinations([i for i in range(len(sents)) if i not in impossible_sents], s + 1)
        for c in combinations:
            candidates_1 = [evaluated_1grams[idx] for idx in c]
            candidates_1 = set.union(*map(set, candidates_1))
            candidates_2 = [evaluated_2grams[idx] for idx in c]
            candidates_2 = set.union(*map(set, candidates_2))
            rouge_1 = cal_rouge(candidates_1, reference_1grams)['f']
            rouge_2 = cal_rouge(candidates_2, reference_2grams)['f']

            rouge_score = rouge_1 + rouge_2
            if (s == 0 and rouge_score == 0):
                impossible_sents.append(c[0])
            if rouge_score > max_rouge:
                max_idx = c
                max_rouge = rouge_score
    return sorted(list(max_idx))


def greedy_selection(doc_sent_list, abstract_sent_list, summary_size):
    def _rouge_clean(s):
        return re.sub(r'[^a-zA-Z0-9 ]', '', s)

    max_rouge = 0.0
    abstract = sum(abstract_sent_list, [])
    abstract = _rouge_clean(' '.join(abstract)).split()
    sents = [_rouge_clean(' '.join(s)).split() for s in doc_sent_list]
    evaluated_1grams = [_get_word_ngrams(1, [sent]) for sent in sents]
    reference_1grams = _get_word_ngrams(1, [abstract])
    evaluated_2grams = [_get_word_ngrams(2, [sent]) for sent in sents]
    reference_2grams = _get_word_ngrams(2, [abstract])

    selected = []
    for s in range(summary_size):
        cur_max_rouge = max_rouge
        cur_id = -1
        for i in range(len(sents)):
            if (i in selected):
                continue
            c = selected + [i]
            candidates_1 = [evaluated_1grams[idx] for idx in c]
            candidates_1 = set.union(*map(set, candidates_1))
            candidates_2 = [evaluated_2grams[idx] for idx in c]
            candidates_2 = set.union(*map(set, candidates_2))
            rouge_1 = cal_rouge(candidates_1, reference_1grams)['f']
            rouge_2 = cal_rouge(candidates_2, reference_2grams)['f']
            rouge_score = rouge_1 + rouge_2
            if rouge_score > cur_max_rouge:
                cur_max_rouge = rouge_score
                cur_id = i
        if (cur_id == -1):
            return selected
        selected.append(cur_id)
        max_rouge = cur_max_rouge

    return sorted(selected)


class BertData():
    def __init__(self, args):
        self.args = args
        self.tokenizer = BertTokenizer.from_pretrained('../bert-base-chinese-vocab.txt', do_lower_case=True)  ### change from 'bert-base-uncased' to 'bert-base-chinese'
        self.sep_vid = self.tokenizer.vocab['[SEP]']
        self.cls_vid = self.tokenizer.vocab['[CLS]']
        self.pad_vid = self.tokenizer.vocab['[PAD]']

    def preprocess(self, src, tgt, oracle_ids):

        if (len(src) == 0):
            return None

        original_src_txt = [' '.join(s) for s in src]

        labels = [0] * len(src)
        for l in oracle_ids:
            labels[l] = 1

        idxs = [i for i, s in enumerate(src) if (len(s) > self.args.min_src_ntokens)]

        src = [src[i][:self.args.max_src_ntokens] for i in idxs]
        labels = [labels[i] for i in idxs]
        src = src[:self.args.max_nsents]
        labels = labels[:self.args.max_nsents]

        if (len(src) < self.args.min_nsents):
            return None
        if (len(labels) == 0):
            return None

        src_txt = [' '.join(sent) for sent in src]
        # text = [' '.join(ex['src_txt'][i].split()[:self.args.max_src_ntokens]) for i in idxs]
        # text = [_clean(t) for t in text]
        text = ' [SEP] [CLS] '.join(src_txt)
        src_subtokens = self.tokenizer.tokenize(text)
        src_subtokens = src_subtokens[:510]
        src_subtokens = ['[CLS]'] + src_subtokens + ['[SEP]']

        src_subtoken_idxs = self.tokenizer.convert_tokens_to_ids(src_subtokens)
        _segs = [-1] + [i for i, t in enumerate(src_subtoken_idxs) if t == self.sep_vid]
        segs = [_segs[i] - _segs[i - 1] for i in range(1, len(_segs))]
        segments_ids = []
        for i, s in enumerate(segs):
            if (i % 2 == 0):
                segments_ids += s * [0]
            else:
                segments_ids += s * [1]
        cls_ids = [i for i, t in enumerate(src_subtoken_idxs) if t == self.cls_vid]
        labels = labels[:len(cls_ids)]

        tgt_txt = '<q>'.join([' '.join(tt) for tt in tgt])
        src_txt = [original_src_txt[i] for i in idxs]
        return src_subtoken_idxs, labels, segments_ids, cls_ids, src_txt, tgt_txt


def format_to_bert(args):
    if (args.dataset != ''):
        datasets = [args.dataset]
    else:
        datasets = ['train', 'valid', 'test']
    for corpus_type in datasets:
        a_lst = []
        for json_f in glob.glob(pjoin(args.raw_path, '*' + corpus_type + '.*.json')):
            real_name = json_f.split('/')[-1]
            a_lst.append((json_f, args, pjoin(args.save_path, real_name.replace('json', 'bert.pt'))))
        print(a_lst)
        pool = Pool(args.n_cpus)
        for d in pool.imap(_format_to_bert, a_lst):
            pass

        pool.close()
        pool.join()


def _format_to_bert(params):
    json_file, args, save_file = params
    if (os.path.exists(save_file)):
        logger.info('Ignore %s' % save_file)
        return

    bert = BertData(args)

    logger.info('Processing %s' % json_file)
    jobs = json.load(open(json_file))
    datasets = []
    for d in jobs:
        source, tgt = d['src'], d['tgt']
        if (args.oracle_mode == 'greedy'):
            oracle_ids = greedy_selection(source, tgt, 3)
        elif (args.oracle_mode == 'combination'):
            oracle_ids = combination_selection(source, tgt, 3)
        b_data = bert.preprocess(source, tgt, oracle_ids)
        if (b_data is None):
            continue
        indexed_tokens, labels, segments_ids, cls_ids, src_txt, tgt_txt = b_data
        b_data_dict = {"src": indexed_tokens, "labels": labels, "segs": segments_ids, 'clss': cls_ids,
                       'src_txt': src_txt, "tgt_txt": tgt_txt}
        datasets.append(b_data_dict)
    logger.info('Saving to %s' % save_file)
    torch.save(datasets, save_file)
    datasets = []
    gc.collect()


def format_to_lines(args):
    train_files, valid_files, test_files = [], [], []
    for f in glob.glob(pjoin(args.raw_path, '*.json')):
        logger.info("path:" + f)
        real_name = f.split('/')[-1].split('.')[0]
        logger.info("real_name:" + real_name)
        with open(f, "r") as read_json:
            data_file = json.load(read_json)
            
        if ('valid' in real_name):
            valid_files = data_file
        elif ('test' in real_name):
            test_files = data_file
        elif ('train' in real_name):
            train_files = data_file

    corpora = {'train': train_files, 'valid': valid_files, 'test': test_files}

    for corpus_type in ['train', 'valid', 'test']:
        dataset = []
        p_ct = 0

        for d in corpora[corpus_type]:
            d_formated = _format_to_lines(d)
            dataset.append(d_formated)
            if (len(dataset) > args.shard_size - 1):
                pt_file = "{:s}.{:s}.{:d}.json".format(args.save_path, corpus_type, p_ct)
                with open(pt_file, 'w') as save:
                    # save.write('\n'.join(dataset))
                    save.write(json.dumps(dataset))
                    p_ct += 1
                    dataset = []

        if (len(dataset) > 0):
            pt_file = "{:s}.{:s}.{:d}.json".format(args.save_path, corpus_type, p_ct)
            with open(pt_file, 'w') as save:
                # save.write('\n'.join(dataset))
                save.write(json.dumps(dataset))
                p_ct += 1
                dataset = []

def _format_to_lines(json_element):
    json_element_split = {'src':sent_token_split(json_element['src']), 'tgt':sent_token_split(json_element['tgt'],True)}
    return json_element_split


def format_raw(args):
    for i in glob.glob(pjoin(args.raw_path, 'PART_*.txt')):
        is_train = True if "PART_I." in i else False
        is_valid = True if "PART_II." in i else False

        raw_formated = _format_raw(i, is_train=is_train)

        if is_train:
            file_name = "LCSTS_train.json"
        elif is_valid:
            file_name = "LCSTS_valid.json"
        else:
            file_name = "LCSTS_test.json"
        
        json.dump(raw_formated, open(pjoin(args.raw_path,file_name),"w"))


def _format_raw(raw_LCSTS_path, is_train=True):
    raw_LCSTS_file = open(raw_LCSTS_path, "r")
    raw_LCSTS_str = raw_LCSTS_file.read()
    raw_LCSTS_str_list = raw_LCSTS_str.split("\n")
    
    num_line_el = 8 if is_train else 9
    extract_line = [0, 2, 5] if is_train else [0, 3, 6]
    num_el = len(raw_LCSTS_str_list)//num_line_el
    
    json_list = []
    for i in range(num_el):
        doc = {"id": raw_LCSTS_str_list[i*num_line_el+extract_line[0]].strip(), 
        "tgt": raw_LCSTS_str_list[i*num_line_el+extract_line[1]].strip(), 
        "src": raw_LCSTS_str_list[i*num_line_el+extract_line[2]].strip()}

        json_list.append(doc)
    
    for i in json_list:
        num = re.findall(r'\d+', i['id'])
        doc_id = int(num[0])
        i['id'] = doc_id
    
    return json_list

def sent_token_split(doc, is_short_summary = False):
    doc_modified = re.sub(r' ', "", doc)
    doc_modified = re.sub(r':\w+:', "", emoji.demojize(doc_modified))
    
    ### if the doc is a very short summary, just don't split sentence
    if is_short_summary:
        doc_split = [list(doc_modified)]
        return doc_split

    doc_modified = re.sub(r'��', "�� ", doc_modified)
    doc_modified = re.sub(r'��', "�� ", doc_modified)
    doc_modified = re.sub(r'��', "�� ", doc_modified)
    
    doc_split = re.split(r' ', doc_modified)
    doc_split = [i for i in doc_split if len(i)>=2]
    
    if len(doc_split)<2:
        doc_modified = re.sub(r'��', "�� ", doc_modified)
        doc_modified = re.sub(r'��', "�� ", doc_modified)
        doc_split = re.split(r' ', doc_modified)
        doc_split = [i for i in doc_split if len(i)>=2]
    
    doc_split = [list(i) for i in doc_split]
    
    return doc_split
