# coding=utf-8
# Copyright 2021-2022, Ontocord, LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# NOTES:
# we want to create feature detectors to segment spans of text. One way is to do clustering of embeddings of text.
# this will roughly correspond to area of similarities or interestingness. 
# we could do changes in perplexity, changes in embedding similarity, and detection of patterns such as section headers.
# we could have heuristics hand-crafted rules, like regexes for ALL CAPs folowed by non ALL CAPS, or regions of low #s of stopwords, followed by high #s of stopwords.
# or regions of high count of numbers ($1000,000).

# we could also run segments of text through counting the "_", based on sentence similarities, etc. and create a series.
# below is a simple detection of change from a std dev from a running mean, but we could do some more complex fitting using:
# the library ruptures. https://centre-borelli.github.io/ruptures-docs/examples/text-segmentation/

# with region labels, we can do things like tf-idf of words, and then do a mean of the tf-idf of a span. A span with high avg tf-idf means it is interesting or relevant. 

import math, os
import copy
import fasttext
from sklearn.cluster import MiniBatchKMeans
from sklearn.cluster import AgglomerativeClustering
from time import time
import numpy as np
from collections import Counter
import kenlm
import statistics
import torch
from transformers import AutoTokenizer, AutoModel, BertTokenizerFast, CLIPProcessor, CLIPModel, BertModel
import torch.nn.functional as F
import random
import spacy
import json
from dateutil.parser import parse as dateutil_parse
import pandas as pd
from snorkel.labeling import labeling_function
import itertools
from nltk.corpus import stopwords as nltk_stopwords
import pickle
from collections import OrderedDict
from fast_pytorch_kmeans import KMeans
import torch

if torch.cuda.is_available():
  device = 'cuda'
else:
  device = 'cpu'

try:
  if minilm_model is not None: 
    pass
except:
   labse_tokenizer= labse_model=  clip_processor = minilm_tokenizer= clip_model= minilm_model= spacy_nlp= stopwords_set = None
  
def np_memmap(f, dat=None, idxs=None, shape=None, dtype=np.float32, ):
  if not f.endswith(".mmap"):
    f = f+".mmap"
  if os.path.exists(f):
    mode = "r+"
  else:
    mode = "w+"
  if shape is None: shape = dat.shape
  memmap = np.memmap(f, mode=mode, dtype=dtype, shape=tuple(shape))
  if dat is None:
    return memmap
  if tuple(shape) == tuple(dat.shape):
    memmap[:] = dat
  else:
    memmap[idxs] = dat
  return memmap

if minilm_model is None:
  clip_processor = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")   
  minilm_tokenizer = AutoTokenizer.from_pretrained('sentence-transformers/all-MiniLM-L6-v2')
  labse_tokenizer = BertTokenizerFast.from_pretrained("setu4993/smaller-LaBSE")


  if device == 'cuda':
    clip_model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32").half().eval()
    minilm_model = AutoModel.from_pretrained('sentence-transformers/all-MiniLM-L6-v2').half().eval()
    labse_model = BertModel.from_pretrained("setu4993/smaller-LaBSE").half().eval()
  else:
    clip_model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32").eval()
    minilm_model = AutoModel.from_pretrained('sentence-transformers/all-MiniLM-L6-v2').eval()
    lbase_model = BertModel.from_pretrained("setu4993/smaller-LaBSE").eval()

  spacy_nlp = spacy.load('en_core_web_md')
  stopwords_set = set(nltk_stopwords.words('english') + ['...', 'could', 'should', 'shall', 'can', 'might', 'may', 'include', 'including'])


class Riverbed:
  def __init__(self):
    pass

  #Mean Pooling - Take attention mask into account for correct averaging
  #TODO, mask out the prefix for data that isn't the first portion of a prefixed text.
  @staticmethod
  def mean_pooling(model_output, attention_mask):
    with torch.no_grad():
      token_embeddings = model_output.last_hidden_state
      input_mask_expanded = attention_mask.unsqueeze(-1).expand(token_embeddings.size()).float()
      return torch.sum(token_embeddings * input_mask_expanded, 1) / torch.clamp(input_mask_expanded.sum(1), min=1e-9)
    
  @staticmethod
  def pp(log_score, length):
    return float((10.0 ** (-log_score / length)))

  def get_perplexity(self,  doc, kenlm_model=None):
    if kenlm_model is None: kenlm_model = {} if not hasattr(self, 'kenlm_model') else self.kenlm_model
    doc_log_score = doc_length = 0
    doc = doc.replace("\n", " ")
    for line in doc.split(". "):
        if "_" in line:
          log_score = min(kenlm_model.score(line),kenlm_model.score(line.replace("_", " ")))
        else:
          log_score = kenlm_model.score(line)
        length = len(line.split()) + 1
        doc_log_score += log_score
        doc_length += length
    return self.pp(doc_log_score, doc_length)

  def tokenize(self, doc, min_compound_weight=0,  compound=None, ngram2weight=None, synonyms=None, use_synonym_replacement=False):
    if synonyms is None: synonyms = {} if not hasattr(self, 'synonyms') else self.synonyms
    if ngram2weight is None: ngram2weight = {} if not hasattr(self, 'ngram2weight') else self.ngram2weight    
    if compound is None: compound = {} if not hasattr(self, 'compound') else self.compound
    if not use_synonym_replacement: synonyms = {} 
    doc = [synonyms.get(d,d) for d in doc.split(" ") if d.strip()]
    len_doc = len(doc)
    for i in range(len_doc-1):
        if doc[i] is None: continue
                
        wordArr = doc[i].strip("_").replace("__", "_").split("_")
        if wordArr[0] in compound:
          max_compound_len = compound[wordArr[0]]
          for j in range(min(len_doc, i+max_compound_len), i+1, -1):
            word = ("_".join(doc[i:j])).strip("_").replace("__", "_")
            wordArr = word.split("_")
            if len(wordArr) <= max_compound_len and word in ngram2weight and ngram2weight.get(word, 0) >= min_compound_weight:
              old_word = word
              doc[j-1] = synonyms.get(word, word).strip("_").replace("__", "_")
              #if old_word != doc[j-1]: print (old_word, doc[j-1])
              for k in range(i, j-1):
                  doc[k] = None
              break
    return (" ".join([d for d in doc if d]))


  #NOTE: we use the '¶' in front of a word to designate a word is a parent in an ontology. 
  #the level of the ontology is determined by the number of '¶'.
  #More '¶' means higher up the ontology. Leaf words have no '¶'
  def get_ontology(self, synonyms=None):
    ontology = {}
    if synonyms is None:
      synonyms = {} if not hasattr(self, 'synonyms') else self.synonyms
    for key, val in synonyms.items():
      ontology[val] = ontology.get(val, []) + [key]
    return ontology

  # find the top parent nodes that have no parents
  def get_top_parents(self, synonyms=None):
    top_parents = []
    if synonyms is None:
      synonyms = {} if not hasattr(self, 'synonyms') else self.synonyms
    
    ontology = self.get_ontology(synonyms)
    for parent in ontology:
      if parent not in synonyms:
        top_parents.append(parent)
    return top_parents

  # cluster one batch of words/vectors, assuming some words have already been clustered
  def cluster_one_batch(self, cluster_vecs, idxs, terms2, true_k, synonyms=None, stopword=None, ngram2weight=None, ):
    global device
    print ('cluster_one_batch', len(idxs))
    if synonyms is None: synonyms = {} if not hasattr(self, 'synonyms') else self.synonyms
    if ngram2weight is None: ngram2weight = {} if not hasattr(self, 'ngram2weight') else self.ngram2weight    
    if stopword is None: stopword = {} if not hasattr(self, 'stopword') else self.stopword
    if device == 'cuda':
      kmeans = KMeans(n_clusters=true_k, mode='cosine')
      km_labels = kmeans.fit_predict(torch.from_numpy(cluster_vecs[idxs]).to(device))
      km_labels = [l.item() for l in km_labels.cpu()]
    else:
      km = MiniBatchKMeans(n_clusters=true_k, init='k-means++', n_init=1,
                                          init_size=max(true_k*3,1000), batch_size=1024).fit(cluster_vecs[idxs])
      km_labels = km.labels_
    ontology = {}
    #print (true_k)
    for term, label in zip(terms2, km_labels):
      ontology[label] = ontology.get(label, [])+[term]
    #print (ontology)
    for key, vals in ontology.items():
      items = [v for v in vals if "_" in v and not v.startswith('¶')]
      if len(items) > 1:
          old_syn_upper =  [synonyms[v] for v in vals if "_" in v and v in synonyms and synonyms[v][1].upper() == synonyms[v][0]]
          old_syn_lower = [synonyms[v] for v in vals if "_" in v and v in synonyms and synonyms[v][1].upper() != synonyms[v][0]]
          items_upper_case = []
          if old_syn_upper:
            old_syn_upper =  Counter(old_syn_upper)
            syn_label = old_syn_upper.most_common(1)[0][0]
            items_upper_case = [v for v in items if (synonyms.get(v) == syn_label) or (synonyms.get(v) is None and v[0].upper() == v[0])]
            for v in copy.copy(items_upper_case):
              for v2 in items:
                if synonyms.get(v)  is None and (v in v2 or v2 in v):
                  items_upper_case.append(v2)
            items_upper_case = list(set(items_upper_case))
            if len(items_upper_case) > 1:
              for word in items_upper_case:
                synonyms[word] = syn_label     
          if old_syn_lower: 
            old_syn_lower =  Counter(old_syn_lower)
            syn_label = old_syn_lower.most_common(1)[0][0]
            items = [v for v in items if synonyms.get(v) in (None, syn_label) and v not in items_upper_case]
            if len(items) > 1:
              for word in items:
                synonyms[word] = syn_label    
          if not old_syn_upper and not old_syn_lower:
            items_upper_case = [v for v in items if v[0].upper() == v[0]]
            for v in copy.copy(items_upper_case):
              for v2 in items:
                if v in v2 or v2 in v:
                  items_upper_case.append(v2)
            items_upper_case = list(set(items_upper_case))
            if len(items_upper_case)  > 1:
              items_upper_case.sort(key=lambda a: ngram2weight.get(a, len(a)))
              syn_label = '¶'+items_upper_case[0]
              for word in items_upper_case:
                synonyms[word] = syn_label
              items = [v for v in items if v not in items_upper_case]
            if len(items) > 1:
              items.sort(key=lambda a: ngram2weight.get(a, len(a)))
              syn_label = '¶'+[a for a in items if a[0].lower() == a[0]][0]
              for word in items:
                synonyms[word] = syn_label
      items = [v for v in vals if v not in synonyms]
      if len(items) > 1:
        items.sort(key=lambda a: ngram2weight.get(a, len(a)))
        parents_only = [a for a in items if a.startswith('¶')]
        if parents_only: 
          label = '¶'+parents_only[0]
          for word in parents_only:
              synonyms[word] = label        
        stopwords_only = [a for a in items if a.lower() in stopword or a in stopwords_set]
        if stopwords_only: 
          label = '¶'+stopwords_only[0]
          for word in stopwords_only:
              synonyms[word] = label
        not_stopwords = [a for a in items if a.lower() not in stopword and a not in stopwords_set]
        if not_stopwords: 
          label = '¶'+not_stopwords[0]
          for word in not_stopwords:
              synonyms[word] = label
    return synonyms

  # create a hiearchical structure given leaves that have already been clustered
  def create_ontology(self, project_name, synonyms=None, stopword=None, ngram2weight=None, words_per_ontology_cluster = 10, kmeans_batch_size=50000, epoch = 10, embed_batch_size=7000, min_prev_ids=10000, embedder="minilm", max_ontology_depth=4, max_top_parents=10000):
    if synonyms is None: synonyms = {} if not hasattr(self, 'synonyms') else self.synonyms
    if ngram2weight is None: ngram2weight = {} if not hasattr(self, 'ngram2weight') else self.ngram2weight    
    if stopword is None: stopword = {} if not hasattr(self, 'stopword') else self.stopword
    # assumes ngram2weight is an ordered dict, ordered roughly by frequency
    if not ngram2weight: return synonyms
    if embedder == "clip":
      embed_dim = clip_model.config.text_config.hidden_size
    elif embedder == "minilm":
      embed_dim = minilm_model.config.hidden_size
    elif embedder == "labse":
      embed_dim = labse_model.config.hidden_size      
    cluster_vecs = np_memmap(f"{project_name}.{embedder}_words", shape=[len(ngram2weight), embed_dim])
    for level in range(max_ontology_depth): 
      terms = list(ngram2weight.keys())
      terms2idx = dict([(term, idx) for idx, term in enumerate(terms)])
      ontology = self.get_ontology(synonyms)
      parents = [parent for parent in ontology.keys() if parent.count('¶') == level + 1]
      cluster_vecs2 = []
      cluster_vecs2_idx = []
      for parent in parents:
        if parent not in ngram2weight:
          cluster = ontology[parent]
          cluster_vecs2.append(np.mean(cluster_vecs[[terms2idx[child] for child in cluster]]))
          cluster_vecs2_idx.append(len(ngram2weight))
          ngram2weight[parent] = statistics.mean([ngram2weight[child] for child in cluster])
      if cluster_vecs2_idx:
        cluster_vecs2 = np.vstack(cluster_vecs2)
        cluster_vecs = np_memmap(f"{project_name}.{embedder}_words", shape=[len(ngram2weight), embed_dim], dat=cluster_vecs2, idxs=cluster_vecs2_idx)  
        cluster_vecs2 = None
        if len(parents) < max_top_parents: continue
        true_k = int(math.sqrt(len(parents)))
        synonyms = self.cluster_one_batch(cluster_vecs, cluster_vecs2_idx, parents, true_k, synonyms=synonyms, stopword=stopword, ngram2weight=ngram2weight, )
    parents = self.get_top_parents()
    cluster_vecs2 = []
    cluster_vecs2_idx = []
    for parent in parents:
        if parent not in ngram2weight:
          cluster = ontology[parent]
          cluster_vecs2.append(np.mean(cluster_vecs[[terms2idx[child] for child in cluster]]))
          cluster_vecs2_idx.append(len(ngram2weight))
          ngram2weight[parent] = statistics.mean([ngram2weight[child] for child in cluster])
    if cluster_vecs2_idx:
        cluster_vecs2 = np.vstack(cluster_vecs2)
        cluster_vecs = np_memmap(f"{project_name}.{embedder}_words", shape=[len(ngram2weight), embed_dim], dat=cluster_vecs2, idxs=cluster_vecs2_idx)  
    return synonyms
 
  
  def create_word_embeds_and_synonyms(self, project_name, synonyms=None, stopword=None, ngram2weight=None, words_per_ontology_cluster = 10, kmeans_batch_size=50000, epoch = 10, embed_batch_size=7000, min_prev_ids=10000, embedder="minilm", max_ontology_depth=4, max_top_parents=10000, do_ontology=True, recluster_type="batch"):
    global clip_model, minilm_model, labse_model
    if synonyms is None: synonyms = {} if not hasattr(self, 'synonyms') else self.synonyms
    if ngram2weight is None: ngram2weight = {} if not hasattr(self, 'ngram2weight') else self.ngram2weight    
    if stopword is None: stopword = {} if not hasattr(self, 'stopword') else self.stopword
    # assumes ngram2weight is an ordered dict, ordered roughly by frequency
    terms = list(ngram2weight.keys())
    if not terms: return synonyms
    if embedder == "clip":
      embed_dim = clip_model.config.text_config.hidden_size
    elif embedder == "minilm":
      embed_dim = minilm_model.config.hidden_size
    elif embedder == "labse":
      embed_dim = labse_model.config.hidden_size
    cluster_vecs = np_memmap(f"{project_name}.{embedder}_words", shape=[len(ngram2weight), embed_dim])
    terms_idx = [idx for idx, term in enumerate(terms) if term not in synonyms and term[0] != '¶' ]
    terms_idx_in_synonyms = [idx for idx, term in enumerate(terms) if term in synonyms and term[0] != '¶']
    len_terms_idx = len(terms_idx)
    #increase the terms_idx list to include non-parent words that have empty embeddings
    for rng in range(0, len(terms_idx), embed_batch_size):
      max_rng = min(len(terms_idx), rng+embed_batch_size)
      if embedder == "clip":
        toks = clip_processor([terms[idx].replace("_", " ") for idx in terms_idx[rng:max_rng]], padding=True, truncation=True, return_tensors="pt").to(device)
        with torch.no_grad():
          cluster_vecs = clip_model.get_text_features(**toks).cpu().numpy()
      elif embedder == "minilm":
        toks = minilm_tokenizer([terms[idx].replace("_", " ") for idx in terms_idx[rng:max_rng]], padding=True, truncation=True, return_tensors="pt").to(device)
        with torch.no_grad():
          cluster_vecs = minilm_model(**toks)
          cluster_vecs = self.mean_pooling(cluster_vecs, toks.attention_mask).cpu().numpy()
      elif embedder == "labse":
        toks = labse_tokenizer([terms[idx].replace("_", " ") for idx in terms_idx[rng:max_rng]], padding=True, truncation=True, return_tensors="pt").to(device)
        with torch.no_grad():
          cluster_vecs = labse_model(**toks).pooler_output.cpu().numpy()          
      cluster_vecs = np_memmap(f"{project_name}.{embedder}_words", shape=[len(terms), cluster_vecs.shape[1]], dat=cluster_vecs, idxs=terms_idx[rng:max_rng])  
    len_terms_idx = len(terms_idx)
    times = -1
    times_start_recluster = max(0, (int(len(terms_idx)/int(kmeans_batch_size*.7))-3))
    for rng in range(0,len_terms_idx, int(kmeans_batch_size*.7)):
      times += 1
      max_rng = min(len_terms_idx, rng+int(kmeans_batch_size*.7))
      prev_ids = [idx for idx in terms_idx[:rng] if terms[idx] not in synonyms]
      terms_idx_in_synonyms.extend([idx for idx in terms_idx[:rng] if terms[idx] in synonyms])
      terms_idx_in_synonyms = list(set(terms_idx_in_synonyms))
      terms_idx_in_synonyms = [idx for idx in terms_idx_in_synonyms if terms[idx] in synonyms]
      max_prev_ids = max(int(kmeans_batch_size*.15), int(.5*min_prev_ids))
      if len(prev_ids) > max_prev_ids:
        prev_ids = random.sample(prev_ids, max_prev_ids)
      avail_prev_ids= 2*max_prev_ids-len(prev_ids)
      if len(terms_idx_in_synonyms) > avail_prev_ids: 
          prev_ids.extend(random.sample(terms_idx_in_synonyms, avail_prev_ids))
      else: 
          prev_ids.extend(terms_idx_in_synonyms)
      idxs = prev_ids + terms_idx[rng:max_rng]
      #print ('clustering', len(idxs))
      true_k=int(max(2, (len(idxs))/words_per_ontology_cluster))
      terms2 = [terms[idx] for idx in idxs]
      synonyms = self.cluster_one_batch(cluster_vecs, idxs, terms2, true_k, synonyms=synonyms, stopword=stopword, ngram2weight=ngram2weight, )
      if times >= times_start_recluster:
        idxs_words=[]
        ontology = self.get_ontology(synonyms)
        max_cluster_size = int(math.sqrt(max_top_parents))
        for key, cluster in ontology.items():
          if max_rng != len_terms_idx and len(cluster) < words_per_ontology_cluster*.5:
            for word in cluster:
              del synonyms[word]
          elif len(cluster) > max_cluster_size:
            #print ('recluster larger to small clusters', key)
            re_cluster = set(cluster)
            for word in cluster:
              del synonyms[word] 
            if recluster_type=="individual":
              idxs_words = [(idx,word) for idx, word in enumerate(ngram2weight.keys()) if word in re_cluster]
              words = [a[1] for a in idxs_words]
              idxs = [a[0] for a in idxs_words]
              true_k=int(max(2, (len(idxs))/words_per_ontology_cluster))
              synonyms = self.cluster_one_batch(cluster_vecs, idxs, words, true_k, synonyms=synonyms, stopword=stopword, ngram2weight=ngram2weight, )    
              idxs_words = []
            else:
              idxs_words.extend([(idx,word) for idx, word in enumerate(ngram2weight.keys()) if word in re_cluster])
              if len(idxs_words) > kmeans_batch_size:
                words = [a[1] for a in idxs_words]
                idxs = [a[0] for a in idxs_words]
                true_k=int(max(2, (len(idxs))/words_per_ontology_cluster))
                synonyms = self.cluster_one_batch(cluster_vecs, idxs, words, true_k, synonyms=synonyms, stopword=stopword, ngram2weight=ngram2weight, )    
                idxs_words = []
        if idxs_words:
                words = [a[1] for a in idxs_words]
                idxs = [a[0] for a in idxs_words]
                true_k=int(max(2, (len(idxs))/words_per_ontology_cluster))
                synonyms = self.cluster_one_batch(cluster_vecs, idxs, words, true_k, synonyms=synonyms, stopword=stopword, ngram2weight=ngram2weight, )    
                idxs_words = []
    if do_ontology: synonyms = self.create_ontology(project_name, synonyms=synonyms, stopword=stopword, ngram2weight=ngram2weight, words_per_ontology_cluster = words_per_ontology_cluster, kmeans_batch_size=50000, epoch = 10, embed_batch_size=embed_batch_size, min_prev_ids=min_prev_ids, embedder=embedder, max_ontology_depth=max_ontology_depth, max_top_parents=max_top_parents)
    return synonyms

  #TODO, strip non_words
  # creating tokenizer with a kenlm model as well as getting ngram weighted by the language modeling weights (not the counts) of the words
  # we can run this in incremental mode or batched mode (just concatenate all the files togehter)
  def create_tokenizer_and_train(self, project_name, files, lmplz_loc="./riverbed/bin/lmplz", stopword_max_len=10, num_stopwords=75, min_compound_word_size=25, max_ontology_depth=4, max_top_parents=10000, \
                lstrip_stopword=False, rstrip_stopword=False, non_words = "،♪↓↑→←━\₨₡€¥£¢¤™®©¶§←«»⊥∀⇒⇔√­­♣️♥️♠️♦️‘’¿*’-ツ¯‿─★┌┴└┐▒∎µ•●°。¦¬≥≤±≠¡×÷¨´:।`~�_“”/|!~@#$%^&*•()【】[]{}-_+–=<>·;…?:.,\'\"", kmeans_batch_size=50000, dedup_compound_words_larger_than=None, \
                embed_batch_size=7000, min_prev_ids=10000, min_compound_weight=1.0, stopword=None, min_num_words=5, do_collapse_values=True, use_synonym_replacement=False, embedder="minilm", do_ontology=True, recluster_type="batch"):
      global device, clip_model, minilm_model, labse_model
      
      if embedder == "clip":
        clip_model = clip_model.to(device)
        minilm_model =  minilm_model.cpu()
        labse_model =  labse_model.cpu()
      elif embedder == "minilm":
        clip_model = clip_model.cpu()
        minilm_model =  minilm_model.to(device)
        labse_model =  labse_model.cpu()
      elif embedder == "labse":
        clip_model = clip_model.cpu()
        minilm_model =  minilm_model.cpu()
        labse_model =  labse_model.to(device)

      ngram2weight = self.ngram2weight = OrderedDict() if not hasattr(self, 'ngram2weight') else self.ngram2weight
      compound = self.compound = {} if not hasattr(self, 'compound') else self.compound
      synonyms = self.synonyms = {} if not hasattr(self, 'synonyms') else self.synonyms
      stopword = self.stopword = {} if not hasattr(self, 'stopword') else self.stopword
      for word in stopwords_set:
        word = word.lower()
        stopword[word] = stopword.get(word, 1.0)
      if lmplz_loc != "./riverbed/bin/lmplz" and not os.path.exists("./lmplz"):
        os.system(f"cp {lmplz_loc} ./lmplz")
        lmplz = "./lmplz"
      else:
        lmplz = lmplz_loc
      os.system(f"chmod u+x {lmplz}")
      unigram = {}
      arpa = {}
      if ngram2weight:
        for word in ngram2weight.keys():
          if "_" not in word: unigram[word] = min(unigram.get(word,0), ngram2weight[word])
      if os.path.exists(f"{project_name}.arpa"):
        with open(f"{project_name}.arpa", "rb") as af:
          n = 0
          do_ngram = False
          for line in af:
            line = line.decode().strip()
            if line.startswith("\\1-grams:"):
              n = 1
              do_ngram = True
            elif line.startswith("\\2-grams:"):
              n = 2
              do_ngram = True
            elif line.startswith("\\3-grams:"):
              n = 3
              do_ngram = True
            elif line.startswith("\\4-grams:"):
              n = 4
              do_ngram = True
            elif line.startswith("\\5-grams:"):
              n = 5
              do_ngram = True
            elif do_ngram:
              line = line.split("\t")
              if len(line) > 1:
                arpa[(n, line[1])] = min(float(line[0]), arpa.get((n, line[1]), 100))
      #TODO, we should try to create consolidated files of around 1GB to get enough information in the arpa files
      for doc_id, file_name in enumerate(files):
        if dedup_compound_words_larger_than:
          dedup_compound_words_num_iter = max(0, math.ceil(dedup_compound_words_larger_than/(5 *(doc_id+1))))
          self.ngram2weight, self.compound, self.synonyms, self.stopword = ngram2weight, compound, synonyms, stopword 
          compound = copy.deepcopy(compound)
          synonyms = copy.deepcopy(synonyms)
          stopword = copy.deepcopy(stopword)
          ngram2weight = copy.deepcopy(ngram2weight)
        else:
          dedup_compound_words_num_iter = 0
        num_iter = max(1,math.ceil(min_compound_word_size/(5 *(doc_id+1))))
        #we can repeatedly run the below to get long ngrams
        #after we tokenize for ngram and replace with words with underscores (the_projected_revenue) at each step, we redo the ngram count
        curr_arpa = {}
        print ('num iter', num_iter, dedup_compound_words_num_iter)
        for times in range(num_iter+dedup_compound_words_num_iter):
            print (f"iter {file_name}", times)
            if times == 0:
              os.system(f"cp {file_name} __tmp__{file_name}")
            elif dedup_compound_words_larger_than is not None and times == dedup_compound_words_num_iter:
              # sometimes we want to do some pre-processing b/c n-grams larger than a certain amount are just duplicates
              # and can mess up our word counts
              print ('deduping compound words larger than',dedup_compound_words_larger_than)
              os.system(f"cp {file_name} __tmp__{file_name}")
              with open(f"__tmp__2_{file_name}", "w", encoding="utf8") as tmp2:
                with open(f"__tmp__{file_name}", "r") as f:
                  deduped_num_words = 0
                  seen_dedup_compound_words = {}
                  for l in f:
                    orig_l = l.replace("_", " ").replace("  ", " ").strip()
                    l = self.tokenize(l.strip(), min_compound_weight=0, compound=compound, ngram2weight=ngram2weight,  synonyms=synonyms, use_synonym_replacement=False)
                    l = l.split()
                    dedup_compound_word = [w for w in l if "_" in w and w.count("_") + 1 > dedup_compound_words_larger_than]
                    if not dedup_compound_word:
                      l2 = " ".join(l).replace("_", " ").strip()
                      tmp2.write(l2+"\n")
                      continue
                    l = [w if ("_" not in w or w.count("_") + 1 <= dedup_compound_words_larger_than or w not in seen_dedup_compound_words) else '...' for w in l]
                    l2 = " ".join(l).replace("_", " ").replace(' ... ...', ' ...').strip()
                    if l2.endswith(" ..."): l2 = l2[:-len(" ...")]
                    if dedup_compound_word and l2 != orig_l:
                      deduped_num_words += 1
                    #  print ('dedup ngram', dedup_compound_word, l2)
                    for w in dedup_compound_word:
                      seen_dedup_compound_words[w] = 1
                    tmp2.write(l2+"\n")
                  seen_dedup_compound_words = None
                  print ('finished deduping', deduped_num_words)
              os.system(f"cp __tmp__2_{file_name} {file_name}.dedup")  
              os.system(f"mv __tmp__2_{file_name} __tmp__{file_name}")   
              ngram2weight = self.ngram2weight = OrderedDict() if not hasattr(self, 'ngram2weight') else self.ngram2weight
              compound = self.compound = {} if not hasattr(self, 'compound') else self.compound
              synonyms = self.synonyms = {} if not hasattr(self, 'synonyms') else self.synonyms
              stopword = self.stopword = {} if not hasattr(self, 'stopword') else self.stopword              
              curr_arpa = {}
            # we only do synonym and embedding creation as the second to last or last step of each file processed 
            # b/c this is very expensive. we can do this right before the last counting if we
            # do synonym replacement so we have a chance to create syonyms for the replacement.
            # otherwise, we do it after the last count. See below.
            synonyms_created=  False          
            if use_synonym_replacement and times == num_iter+dedup_compound_words_num_iter-1 and ngram2weight:
                synonyms_created = True
                self.synonyms = synonyms = self.create_word_embeds_and_synonyms(project_name, stopword=stopword, ngram2weight=ngram2weight, synonyms=synonyms, kmeans_batch_size=kmeans_batch_size, \
                  embedder=embedder, embed_batch_size=embed_batch_size, min_prev_ids=min_prev_ids, max_ontology_depth=max_ontology_depth, max_top_parents=max_top_parents, do_ontology=do_ontology, recluster_type=recluster_type)   
            if ngram2weight:
              with open(f"__tmp__2_{file_name}", "w", encoding="utf8") as tmp2:
                with open(f"__tmp__{file_name}", "r") as f:
                  for l in f:
                    l = self.tokenize(l.strip(),  min_compound_weight=min_compound_weight, compound=compound, ngram2weight=ngram2weight, synonyms=synonyms, use_synonym_replacement=use_synonym_replacement)
                    if times == num_iter-1:
                      l = self.tokenize(l.strip(), min_compound_weight=0, compound=compound, ngram2weight=ngram2weight,  synonyms=synonyms, use_synonym_replacement=use_synonym_replacement)
                    tmp2.write(l+"\n")  
              os.system(f"mv __tmp__2_{file_name} __tmp__{file_name}")  
            if do_collapse_values:
              os.system(f"./{lmplz} --collapse_values  --discount_fallback  --skip_symbols -o 5 --prune {min_num_words}  --arpa {file_name}.arpa <  __tmp__{file_name}") ##
            else:
              os.system(f"./{lmplz}  --discount_fallback  --skip_symbols -o 5 --prune {min_num_words}  --arpa {file_name}.arpa <  __tmp__{file_name}") ##
            do_ngram = False
            n = 0
            with open(f"{file_name}.arpa", "rb") as f:    
              for line in  f: 
                line = line.decode().strip()
                if not line: 
                  continue
                if line.startswith("\\1-grams:"):
                  n = 1
                  do_ngram = True
                elif line.startswith("\\2-grams:"):
                  n = 2
                  do_ngram = True
                elif line.startswith("\\3-grams:"):
                  n = 3
                  do_ngram = True
                elif line.startswith("\\4-grams:"):
                  n = 4
                  do_ngram = True
                elif line.startswith("\\5-grams:"):
                  n = 5
                  do_ngram = True
                elif do_ngram:
                  line = line.split("\t")
                  try:
                    weight = float(line[0])
                  except:
                    continue                  
                  if len(line) > 1:
                    key = (n, line[1])
                    curr_arpa[key] = min(curr_arpa.get(key,100), weight)
                  #print (line
                  weight = math.exp(weight)
                  line = line[1]
                  if not line: continue
                  line = line.split()
                  if [l for l in line if l in non_words or l in ('<unk>', '<s>', '</s>')]: continue
                  if not(len(line) == 1 and line[0] in stopword):
                    if lstrip_stopword:
                      while line:
                        if line[0].lower() in stopword:
                          line = line[1:]
                        else:
                          break
                    if rstrip_stopword:
                      while line:
                        if line[-1].lower() in stopword:
                          line = line[:-1]
                        else:
                          break
                  word = "_".join(line)
                  if word.startswith('¶') and word not in ngram2weight: #unless this word is a parent synonym, we will strip our special prefix
                    word = word.lstrip('¶')
                  wordArr = word.split("_")
                  if wordArr[0]  in ('<unk>', '<s>', '</s>', ''):
                    wordArr = wordArr[1:]
                  if wordArr[-1]  in ('<unk>', '<s>', '</s>', ''):
                    wordArr = wordArr[:-1]
                  if wordArr:
                    # we are prefering stopwords that starts an n-gram. 
                    if (not lstrip_stopword or len(wordArr) == 1) and len(wordArr[0]) <= stopword_max_len:
                      sw = wordArr[0].lower()
                      unigram[sw] = min(unigram.get(sw,100), weight)
                      
                    #create the compound words length data structure
                    if weight >= min_compound_weight:
                      compound[wordArr[0]] = max(len(wordArr), compound.get(wordArr[0],0))
                    weight = weight * len(wordArr)            
                    ngram2weight[word] = min(ngram2weight.get(word, 100), weight) 
            top_stopword={} 
            if unigram:
                stopword_list = [l for l in unigram.items() if len(l[0]) > 0]
                stopword_list.sort(key=lambda a: a[1])
                len_stopword_list = len(stopword_list)
                top_stopword = stopword_list[:min(len_stopword_list, num_stopwords)]
            for word, weight in top_stopword:
              stopword[word] = min(stopword.get(word, 100), weight)
            os.system(f"rm {file_name}.arpa")
            if times == num_iter+dedup_compound_words_num_iter-1  and not synonyms_created:
                self.synonyms = synonyms = self.create_word_embeds_and_synonyms(project_name, stopword=stopword, ngram2weight=ngram2weight, synonyms=synonyms, kmeans_batch_size=kmeans_batch_size, \
                  embedder=embedder, embed_batch_size=embed_batch_size, min_prev_ids=min_prev_ids, max_ontology_depth=max_ontology_depth, max_top_parents=max_top_parents, do_ontology=do_ontology, recluster_type=recluster_type)   
        for key, weight in curr_arpa.items():
            arpa[key] = min(float(weight), arpa.get(key, 100))
        curr_arpa = {}
      print ('len syn', len(synonyms))


      self.ngram2weight, self.compound, self.synonyms, self.stopword = ngram2weight, compound, synonyms, stopword 
      print ('counting arpa')
      ngram_cnt = [0]*5
      for key in arpa.keys():
        n = key[0]-1
        ngram_cnt[n] += 1
      print ('printing arpa')
      #output the final kenlm .arpa file for calculating the perplexity
      with open(f"__tmp__.arpa", "w", encoding="utf8") as tmp_arpa:
        tmp_arpa.write("\\data\\\n")
        tmp_arpa.write(f"ngram 1={ngram_cnt[0]}\n")
        tmp_arpa.write(f"ngram 2={ngram_cnt[1]}\n")
        tmp_arpa.write(f"ngram 3={ngram_cnt[2]}\n")
        tmp_arpa.write(f"ngram 4={ngram_cnt[3]}\n")
        tmp_arpa.write(f"ngram 5={ngram_cnt[4]}\n")
        for i in range(5):
          tmp_arpa.write("\n")
          j =i+1
          tmp_arpa.write(f"\\{j}-grams:\n")
          for key, val in arpa.items():
            n, dat = key
            if n != j: continue
            if val > 0:
              val =  0
            tmp_arpa.write(f"{val}\t{dat}\t0\n")
        tmp_arpa.write("\n\\end\\\n\n")
      os.system(f"mv __tmp__.arpa {project_name}.arpa")
      print ('creating kenlm model')
      self.kenlm_model = kenlm.LanguageModel(f"{project_name}.arpa") 
      os.system("rm -rf __tmp__*")
      return {'ngram2weight':ngram2weight, 'compound': compound, 'synonyms': synonyms, 'stopword': stopword,  'kenlm_model': self.kenlm_model} 

  ################
  # SPAN BASED CODE
  # includes labeling of spans of text with different features, including clustering
  # assumes each batch is NOT shuffeled.


  @staticmethod
  def dateutil_parse_ext(text):
    try: 
      int(text.strip())
      return None
    except:
      pass
    try:
      text = text.replace("10-K", "")
      ret= dateutil_parse(text.replace("-", " "), fuzzy_with_tokens=True)
      if type(ret) is tuple: ret = ret[0]
      return ret.strftime('%x').strip()
    except:
      return None

  def intro_with_date(self, span):
    text, position, ents = span['text'], span['position'], span['ents']
    if position < 0.05 and text.strip() and (len(text) < 50 and text[0] not in "0123456789" and text[0] == text[0].upper() and text.split()[-1][0] == text.split()[-1][0].upper()):
      date = [e[0] for e in ents if e[1] == 'DATE']
      if date: 
        date = date[0]
        date = self.dateutil_parse_ext(date)
      if  date: 
        return 'intro: date of '+ date +"; "+text + " || "
      else:
        return 'intro: ' +text + " || "

  def section_with_date(self, span):
    text, position, ents = span['text'], span['position'], span['ents']
    if  position >= 0.05 and position < 0.95 and text.strip() and (len(text) < 50 and text[0] not in "0123456789" and text[0] == text[0].upper() and text.split()[-1][0] == text.split()[-1][0].upper()):
      date = [e[0] for e in ents if e[1] == 'DATE']
      if date: 
        date = date[0]
        date = self.dateutil_parse_ext(date)
      if  date: 
        return 'section: date of '+ date +"; "+text + " || "
      else:
        return  'section: ' +text + " || "
    return None

  def conclusion_with_date(self, span):
    text, position, ents = span['text'], span['position'], span['ents']
    if  position >= 0.95 and text.strip() and (len(text) < 50 and text[0] not in "0123456789" and text[0] == text[0].upper() and text.split()[-1][0] == text.split()[-1][0].upper()):
      date = [e[0] for e in ents if e[1] == 'DATE']
      if date: 
        date = date[0]
        date = self.dateutil_parse_ext(date)
      if  date: 
        return 'conclusion: date of '+ date +"; "+text + " || "
      else:
        return 'conclusion: ' +text + " || "
    return None


  RELATIVE_LOW = 0
  RELATIVE_MEDIUM = 1
  RELATIVE_HIGH= 2
  # for extracting a prefix for a segment of text. a segment can contain multiple spans.
  default_prefix_extractors = [
      ('intro_with_date', intro_with_date), \
      ('section_with_date', section_with_date), \
      ('conclusion_with_date', conclusion_with_date) \
      ]

  # for feature extraction on a single span and potentially between spans in a series. 
  # tuples of (feature_label, lower_band, upper_band, extractor). assumes prefix extraction has occured.
  # returns data which can be used to store in the feature_label for a span. if upper_band and lower_band are set, then an additional label X_level stores
  # the relative level label as well.
  #
  #TODO: other potential features include similarity of embedding from its cluster centroid
  #compound words %
  #stopwords %
  #tf-idf weight
  
  default_span_level_feature_extractors = [
      ('perplexity', .5, 1.5, lambda self, span: 0.0 if self.kenlm_model is None else self.get_perplexity(span['tokenized_text'])),
      ('prefix', None, None, lambda self, span: "" if " || " not in span['text'] else  span['text'].split(" || ", 1)[0].strip()),
      ('date', None, None, lambda self, span: "" if " || " not in span['text'] else span['text'].split(" || ")[0].split(":")[-1].split("date of")[-1].strip("; ")), 
  ]

  # for labeling the spans in the batch. assumes feature extractions above. (span_label, snorkel_labling_lfs, snorkel_label_cardinality, snorkel_epochs)
  default_lfs = []

  # the similarity models sometimes put too much weight on proper names, etc. but we might want to cluster by general concepts
  # such as change of control, regulatory actions, etc. The proper names themselves can be collapsed to one canonical form (The Person). 
  # Similarly, we want similar concepts (e.g., compound words) to cluster to one canonical form.
  # we do this by collapsing to an NER label and/or creating a synonym map from compound words to known words. See create_ontology_and_synonyms
  # and we use that data to simplify the sentence here.  
  # TODO: have an option NOT to simplify the prefix. 
  def simplify_text(self, text, ents, ner_to_simplify=(), use_synonym_replacement=False):
    ngram2weight, compound, synonyms  = self.ngram2weight, self.compound, self.synonyms
    if not ner_to_simplify and not synonyms and not ents: return text, ents
    # assumes the text has already been tokenized and replacing NER with @#@{idx}@#@ 
    tokenized_text = text
    #do a second tokenize if we want to do synonym replacement.
    if use_synonym_replacement:
      tokenized_text = self.tokenize(text, use_synonym_replacement=True)  
    ents2 = []

    for idx, ent in enumerate(ents):
        entity, label = ent
        if "@#@" not in text: break
        if f"@#@{idx}@#@" not in text: continue
        text = text.replace(f"@#@{idx}@#@", entity) 
    text = text.replace("_", " ")

    for idx, ent in enumerate(ents):
        entity, label = ent
        if "@#@" not in tokenized_text: break
        if f"@#@{idx}@#@" not in tokenized_text: continue
        ents2.append((entity, label,  text.count(f"@#@{idx}@#@")))
        if label in ner_to_simplify:   
          if label == 'ORG':
            tokenized_text = tokenized_text.replace(f"@#@{idx}@#@", 'The Organization')
          elif label == 'PERSON':
            tokenized_text = tokenized_text.replace(f"@#@{idx}@#@", 'The Person')
          elif label == 'FAC':
            tokenized_text = tokenized_text.replace(f"@#@{idx}@#@", 'The Facility')
          elif label in ('GPE', 'LOC'):
            tokenized_text = tokenized_text.replace(f"@#@{idx}@#@", 'The Location')
          elif label in ('DATE', ):
            tokenized_text = tokenized_text.replace(f"@#@{idx}@#@", 'The Date')
          elif label in ('LAW', ):
            tokenized_text = tokenized_text.replace(f"@#@{idx}@#@", 'The Law')  
          elif label in ('EVENT', ):
            tokenized_text = tokenized_text.replace(f"@#@{idx}@#@", 'The Event')            
          elif label in ('MONEY', ):
            tokenized_text = tokenized_text.replace(f"@#@{idx}@#@", 'The Amount')
          else:
            tokenized_text = tokenized_text.replace(f"@#@{idx}@#@", entity.replace(" ", "_"))
        else:
          tokenized_text = tokenized_text.replace(f"@#@{idx}@#@", entity.replace(" ", "_"))    

    for _ in range(3):
      tokenized_text = tokenized_text.replace("The Person and The Person", "The Person").replace("The Person The Person", "The Person").replace("The Person, The Person", "The Person")
      tokenized_text = tokenized_text.replace("The Facility and The Facility", "The Facility").replace("The Facility The Facility", "The Facility").replace("The Facility, The Facility", "The Facility")
      tokenized_text = tokenized_text.replace("The Organization and The Organization", "The Organization").replace("The Organization The Organization", "The Organization").replace("The Organization, The Organization", "The Organization")
      tokenized_text = tokenized_text.replace("The Location and The Location", "The Location").replace("The Location The Location", "The Location").replace("The Location, The Location", "The Location")
      tokenized_text = tokenized_text.replace("The Date and The Date", "The Date").replace("The Date The Date", "The Date").replace("The Date, The Date", "The Date")
      tokenized_text = tokenized_text.replace("The Law and The Law", "The Law").replace("The Law The Law", "The Law").replace("The Law, The Law", "The Law")
      tokenized_text = tokenized_text.replace("The Event and The Event", "The Event").replace("The Event The Event", "The Event").replace("The Event, The Event", "The Event")
      tokenized_text = tokenized_text.replace("The Amount and The Amount", "The Amount").replace("The Amount The Amount", "The Amount").replace("The Amount, The Amount", "The Amount")
      
    return text, tokenized_text, ents2
  
  #transform a doc batch into a span batch, breaking up doc into spans
  #all spans/leaf nodes of a cluster are stored as a triple of (file_name, lineno, offset)
  def create_spans_batch(self, curr_file_size, batch, text_span_size=1000, ner_to_simplify=(), use_synonym_replacement=False):
      ngram2weight, compound, synonyms  = self.ngram2weight, self.compound, self.synonyms
      batch2 = []
      for idx, span in enumerate(batch):
        file_name, curr_lineno, ents, text  = span['file_name'], span['lineno'], span['ents'], span['text']
        for idx, ent in enumerate(ents):
          text = text.replace(ent[0], f' @#@{idx}@#@ ')
        # we do placeholder replacement tokenize to make ngram words underlined, so that we don't split a span in the middle of an ner word or ngram.
        text  = self.tokenize(text, use_synonym_replacement=False) 
        len_text = len(text)
        prefix = ""
        if "||" in text:
          prefix, _ = text.split("||",1)
          prefix = prefix.strip()
        offset = 0
        while offset < len_text:
          max_rng  = min(len_text, offset+text_span_size+1)
          if text[max_rng-1] != ' ':
            # extend for non english periods and other punctuations
            if '. ' in text[max_rng:]:
              max_rng = max_rng + text[max_rng:].index('. ')+1
            elif ' ' in text[max_rng:]:
              max_rng = max_rng + text[max_rng:].index(' ')
            else:
              max_rng = len_text
          if prefix and offset > 0:
            text2 = prefix +" || ... " + text[offset:max_rng].strip().replace("_", " ").replace("  ", " ").replace("  ", " ")
          else:
            text2 = text[offset:max_rng].strip().replace("_", " ").replace("  ", " ").replace("  ", " ")
          text2, tokenized_text, ents2 = self.simplify_text(text2, ents, ner_to_simplify, use_synonym_replacement=use_synonym_replacement) 
          if prefix and offset > 0:
            _, text2 = text2.split(" || ... ", 1)
          sub_span = copy.deepcopy(span)
          sub_span['position'] += offset/curr_file_size
          sub_span['offset'] = offset
          sub_span['text'] = text2
          sub_span['tokenized_text'] = tokenized_text 
          sub_span['ents'] = ents2
          batch2.append(sub_span)
          offset = max_rng

      return batch2

  def create_cluster_for_spans(self, true_k, batch_id_prefix, spans, cluster_vecs, tmp_clusters, span2cluster_label,  idxs, span_per_cluster=20, kmeans_batch_size=1024, ):
    global device
    if device == 'cuda':
      kmeans = KMeans(n_clusters=true_k, mode='cosine')
      km_labels = kmeans.fit_predict(torch.from_numpy(cluster_vecs[idxs]).to(device))
      km_labels = [l.item() for l in km_labels.cpu()]
    else:
      km = MiniBatchKMeans(n_clusters=true_k, init='k-means++', n_init=1,
                                    init_size=max(true_k*3,1000), batch_size=1024).fit(cluster_vecs[idxs])
      km_labels = km.labels_
      
    new_cluster = {}
    for span, label in zip(spans, km_labels):
      label = batch_id_prefix+str(label)
      new_cluster[label] = new_cluster.get(label, [])+[span]
      
    if not tmp_clusters: 
      tmp_clusters = new_cluster
      for label, items in tmp_clusters.items():
        for span in items:
          span2cluster_label[span] = label
    else:
      for label, items in new_cluster.items():
        cluster_labels = [span2cluster_label[span] for span in items if span in span2cluster_label]
        items2 = [span for span in items if span not in span2cluster_label]
        if cluster_labels:
          most_common = Counter(cluster_labels).most_common(1)[0]
          if most_common[1] >= 2: #if two or more of the span in a cluster has already been labeled, use that label for the rest of the spans
            label = most_common[0]
            items = [span for span in items if span2cluster_label.get(span) in (label, None)]
          else:
            items = items2
        else:
          items = items2
        for span in items:
          if span not in tmp_clusters.get(label, []):
              tmp_clusters[label] = tmp_clusters.get(label, []) + [span]
          span2cluster_label[span] = label
    return tmp_clusters, span2cluster_label 

  def create_span_features(self, batch, span_level_feature_extractors, running_features_per_label, running_features_size):
    feature_labels = []
    features = []
    relative_levels = []
    for feature_label, lower_band, upper_band, extractor in span_level_feature_extractors:
      need_to_high = True
      need_to_low = True
      need_to_medium = True
      prior_change = -1
      feature_labels.append(feature_label)
      features.append([])
      relative_levels.append([])
      features_per_label = features[-1]
      relative_level_per_label = relative_levels[-1]
      running_features = running_features_per_label[feature_label] = running_features_per_label.get(feature_label, [])
      if lower_band is not None:
        if len(running_features) < running_features_size:
          for span in batch:
            p = extractor(self, span)
            running_features.append(p)
            if len(running_features) >= running_features_size:
                break
        stdv = statistics.stdev(running_features)
        mn = statistics.mean (running_features)
        relative_label = self.RELATIVE_LOW
      for idx, span in enumerate(batch):
        p = extractor(self, span)
        features_per_label.append(p)
        if lower_band is not None:
          running_features.append(p)
          if len(running_features) >= running_features_size:    
            stdv = statistics.stdev(running_features)
            mn = statistics.mean (running_features)
          if len(running_features) > running_features_size:
            running_features.pop()    
          if abs(p-mn) >= stdv*upper_band and need_to_high:
            relative_label = self.RELATIVE_HIGH
            prior_change = idx
            need_to_high = False
            need_to_low = True
            need_to_medium = True
          elif  abs(p-mn) < stdv*upper_band and abs(p-mn) > stdv*lower_band  and need_to_medium:
            relative_label = self.RELATIVE_MEDIUM
            prior_change = idx
            need_to_high = True
            need_to_low = True
            need_to_medium = False
          elif abs(p-mn) <= stdv*lower_band and need_to_low:
            relative_label = self.RELATIVE_LOW
            prior_change = idx
            need_to_high = False
            need_to_low = True
            need_to_medium = False
          running_features.append(p)
          relative_level_per_label.append(relative_label) 
          
    for idx, span in enumerate(batch):
      span['cluster_label']= None
      span['cluster_label_before']= None
      span['cluster_label_after']= None
      for feature_label, features_per_label, relative_level_per_label in  zip(feature_labels, features, relative_levels):
        span[feature_label] = features_per_label[idx]
        if relative_level_per_label: span[feature_label+"_level"] = relative_level_per_label[idx]
      ent_cnts = Counter(v[1].lower()+"_cnt" for v in span['ents'])
      for feature_label, cnt in ent_cnts.items():
        span[feature_label] = cnt
    return batch

  def create_informative_label_and_tfidf(self, batch, batch_id_prefix, tmp_clusters, span2idx, tmp_span2batch, span2cluster_label, label2tf=None, df=None, domain_stopword_set=stopwords_set,):
    ngram2weight, compound, synonyms, kenlm_model  = self.ngram2weight, self.compound, self.synonyms, self.kenlm_model
    # code to compute tfidf and more informative labels for the span clusters
    if label2tf is None: label2tf = {}
    if df is None: df = {}
    label2label = {}
    #we gather info for tf-idf with respect to each word in each clusters
    for label, values in tmp_clusters.items(): 
      if label.startswith(batch_id_prefix):
        for item in values:
          if span in span2idx:
            span = tmp_span2batch[span]
            text = span['tokenized_text']
            #we don't want the artificial labels to skew the tf-idf calculations
            text = text.replace('The Organization','').replace('The_Organization','')
            text = text.replace('The Person','').replace('The_Person','')
            text = text.replace('The Facility','').replace('The_Facility','')
            text = text.replace('The Location','').replace('The_Location','')          
            text = text.replace('The Date','').replace('The_Date','')
            text = text.replace('The Law','').replace('The_Law','')
            text = text.replace('The Amount','').replace('The_Amount','')
            text = text.replace('The Event','').replace('The_Event','')
            #we add back the entities we had replaced with the artificial labels into the tf-idf calculations
            ents =  list(itertools.chain(*[[a[0].replace(" ", "_")]*a[-1] for a in span['ents']]))
            if span['offset'] == 0:
              if "||" in text:
                prefix, text = text.split("||",1)
                prefix = prefix.split(":")[-1].split(";")[-1].strip()
                text = prefix.split() + text.replace("(", " ( ").replace(")", " ) ").split() + ents
              else:
                 text = text.replace("(", " ( ").replace(")", " ) ").split() + ents
            else:
              text = text.split("||",1)[-1].strip().split() + ents
            len_text = len(text)
            text = [a for a in text if len(a) > 1 and ("_" not in a or (a.count("_")+1 != len([b for b in a.lower().split("_") if  b in domain_stopword_set])))  and a.lower() not in domain_stopword_set and a[0].lower() in "abcdefghijklmnopqrstuvwxyz"]
            cnts = Counter(text)
            aHash = label2tf[label] =  label2tf.get(label, {})
            for word, cnt in cnts.items():
              aHash[word] = cnt/len_text
            for word in cnts.keys():
              df[word] = df.get(word,0) + 1
      
    #Now, acually create a new label from the tfidf of the words in this cluster
    #TODO, see how we might save away the tf-idf info as features, then we would need to recompute the tfidf if new items are added to cluster
    label2label = {}
    for label, tf in label2tf.items():
      if label.startswith(batch_id_prefix):
        tfidf = copy.copy(tf)    
        for word in list(tfidf.keys()):
          tfidf[word]  = tfidf[word] * min(1.5, ngram2weight.get(word, 1)) * math.log(1.0/(1+df[word]))
        top_words2 = [a[0].lower().strip("~!@#$%^&*()<>,.:;")  for a in Counter(tfidf).most_common(min(len(tfidf), 40))]
        top_words2 = [a for a in top_words2 if a not in domain_stopword_set and ("_" not in a or (a.count("_")+1 != len([b for b in a.split("_") if  b in domain_stopword_set])))]
        top_words = []
        for t in top_words2:
          if t not in top_words:
            top_words.append(t)
        if top_words:
          if len(top_words) > 5: top_words = top_words[:5]
          label2 = ", ".join(top_words) 
          label2label[label] = label2
          
    #swap out the labels
    for old_label, new_label in label2label.items():
      if new_label != old_label:
        if old_label in tmp_clusters:
          a_cluster = tmp_span2batch[old_label]
          for item in a_cluster:
            span2cluster_label[item] = new_label
        label2tf[new_label] =  copy.copy(label2tf.get(old_label, {}))
        del label2tf[old_label] 
    for label, values in tmp_clusters.items():          
      spans = [span for span in values if span in span2idx]
      for span in spans:
        tmp_span2batch[span]['cluster_label'] = label
        
    # add before and after label as additional features
    prior_b = None
    for b in batch:
      if prior_b is not None:
        b['cluster_label_before'] = prior_b['cluster_label']
        prior_b['cluster_label_after'] = b['cluster_label']
      prior_b = b
      
    return batch, label2tf, df
  
  # similar to create_word_embeds_and_synonyms, except for spans     
  #(1) compute features and embeddings in one batch for tokenized text.
  #(2) create clusters in an incremental fashion from batch
  #all leaf nodes are spans
  #spanf2idx is a mapping from the span to the actual underlying storage idx (e.g., a jsonl file or database)
  #span2cluster_label is like the synonym data-structure for words.
  def create_span_embeds_and_span2cluster_label(self, project_name, curr_file_size, jsonl_file_idx, span2idx, batch, retained_batch, \
                                                      jsonl_file, batch_id_prefix, span_lfs,  span2cluster_label, \
                                                      text_span_size=1000, kmeans_batch_size=50000, epoch = 10, \
                                                      embed_batch_size=7000, min_prev_ids=10000, embedder="minilm", \
                                                      max_ontology_depth=4, max_top_parents=10000, do_ontology=True, \
                                                      running_features_per_label={}, ner_to_simplify=(), span_level_feature_extractors=default_span_level_feature_extractors, \
                                                      running_features_size=100, label2tf=None, df=None, domain_stopword_set=stopwords_set,\
                                                      verbose_snrokel=False,  span_per_cluster=10, use_synonym_replacement=False, ):
    ngram2weight, compound, synonyms, kenlm_model  = self.ngram2weight, self.compound, self.synonyms, self.kenlm_model
    
    #transform a doc batch into a span batch, breaking up doc into spans
    batch = self.create_spans_batch(curr_file_size, batch, text_span_size=text_span_size, ner_to_simplify=ner_to_simplify, use_synonym_replacement=use_synonym_replacement)
    
    #create features, assuming linear spans.
    batch = self.create_span_features(batch, span_level_feature_extractors, running_features_per_label, running_features_size)
    
    #add the current back to the span2idx data structure
    start_idx_for_curr_batch = len(span2idx)
    tmp_span2batch = {}
    tmp_idx2span = {}
    tmp_batch_idx_in_span2cluster = []
    tmp_batch_idx_not_in_span2cluster = []
    for b in retained_batch + batch :
      span = (b['file_name'], b['lineno'], b['offset'])
      tmp_span2batch[span] = b
      if span not in span2idx:
        b['idx']= span2idx[span] = len(span2idx)
      else:
        b['idx']= span2idx[span]
      if b['idx'] in span2cluster_label:
        tmp_batch_idx_in_span2cluster.append(b['idx'])
      else:
        tmp_batch_idx_not_in_span2cluster.append(b['idx'])
      tmp_idx2span[b['idx']] = span
      
    if embedder == "clip":
      embed_dim = clip_model.config.text_config.hidden_size
    elif embedder == "minilm":
      embed_dim = minilm_model.config.hidden_size
    elif embedder == "labse":
      embed_dim = labse_model.config.hidden_size
    cluster_vecs = np_memmap(f"{project_name}.{embedder}_spans", shape=[len(span2idx), embed_dim])

    for rng in range(0, len(batch), embed_batch_size):
      max_rng = min(len(batch), rng+embed_batch_size)
      if embedder == "clip":
        toks = clip_processor([a['tokenized_text'].replace("_", " ") for a in batch[rng:max_rng]], padding=True, truncation=True, return_tensors="pt").to(device)
        with torch.no_grad():
          cluster_vecs = clip_model.get_text_features(**toks).cpu().numpy()
      elif embedder == "minilm":
        toks = minilm_tokenizer([a['tokenized_text'].replace("_", " ") for a in batch[rng:max_rng]], padding=True, truncation=True, return_tensors="pt").to(device)
        with torch.no_grad():
          cluster_vecs = minilm_model(**toks)
          cluster_vecs = self.mean_pooling(cluster_vecs, toks.attention_mask).cpu().numpy()
      elif embedder == "labse":
        toks = labse_tokenizer([a['tokenized_text'].replace("_", " ") for a in batch[rng:max_rng]], padding=True, truncation=True, return_tensors="pt").to(device)
        with torch.no_grad():
          cluster_vecs = labse_model(**toks).pooler_output.cpu().numpy()  
      cluster_vecs = np_memmap(f"{project_name}.{embedder}_spans", shape=[len(span2idx), embed_dim],  dat=cluster_vecs, idxs=range(len(span2idx)-len(batch)+rng, len(span2idx)-len(batch)+max_rng))  
    
    len_batch = len(tmp_batch_idx_not_in_span2cluster)
    for rng in range(0, len_batch, int(kmeans_batch_size*.7)):
        max_rng = min(len_batch, rng+int(kmeans_batch_size*.7))
        if rng > 0:
          prev_ids = [idx for idx in tmp_batch_idx_not_in_span2cluster[:rng] if tmp_idx2span[idx] not in span2cluster_label]
          tmp_batch_idx_in_span2cluster.extend( [idx for idx in tmp_batch_idx_not_in_span2cluster[:rng] if tmp_idx2span[idx] in span2cluster_label])
          tmp_batch_idx_in_span2cluster = list(set(tmp_batch_idx_in_span2cluster))
          if len(prev_ids) > kmeans_batch_size*.3: prev_ids.extend(random.sample(range(0, rng), (kmeans_batch_size*.3)-len(prev_ids)))
          #TODO: add some more stuff from tmp_batch_idx_in_span2cluster
        else:
          prev_ids = []
        idxs = prev_ids + [tmp_batch_idx_not_in_span2cluster[idx] for idx in range(rng, max_rng)]
        print (len(idxs))
        true_k=int((len(idxs)/span_per_cluster))
        spans2 = [tmp_idx2span[idx] or idx in idxs]
        tmp_clusters, span2cluster_label = self.create_cluster_for_spans(true_k, batch_id_prefix, spans2, cluster_vecs, tmp_clusters, idxs, span2cluster_label, span_per_cluster=span_per_cluster, domain_stopword_set=domain_stopword_set)
        # TODO: recluster
    
    # TODO: create_span_ontology
                   
    # create more informative labels                   
    batch, label2tf, df = self.create_informative_label_and_tfidf(batch, batch_id_prefix, tmp_clusters, span2idx, tmp_span2batch, span2cluster_label, label2tf, df)
    
    # at this point, batch should have enough data for all snorkel labeling functions
    if span_lfs:
      df_train = pd.DataFrom(batch)
      for span_label, lfs, snorkel_label_cardinality, snorkel_epochs in span_lfs:
        # we assume there is no shuffling, so we can tie back to the original batch
        applier = PandasLFApplier(lfs=fs)
        L_train = applier.apply(df=df_train)
        label_model = LabelModel(cardinality=snorkel_label_cardinality, verbose=verbose_snrokel)
        label_model.fit(L_train=L_train,n_epochs=snorkel_epochs)
        for idx, label in enumerate(label_model.predict(L=L_train,tie_break_policy="abstain")):
          batch[idx][span_label] = label
        # note, we only use these models once, since we are doing this in an incremental fashion.
        # we would want to create a final model by training on all re-labeled data from the jsonl file
    
    # all labeling and feature extraction is complete, and the batch has all the info. now save away the batch
    for b in batch:
      if b['idx'] >= start_idx_for_curr_batch:
        jsonl_file.write(json.dumps(b)+"\n")
        #TODO, replace with a datastore abstraction, such as sqlite
    
    # add stuff to the retained batches
                   
    return retained_batch, span2idx, span2cluster_label, label2tf, df   



  def apply_span_feature_detect_and_labeling(self, project_name, files, text_span_size=1000, max_lines_per_section=10, max_len_for_prefix=100, min_len_for_prefix=20, embed_batch_size=100, 
                                                features_batch_size = 10000000, kmeans_batch_size=1024, \
                                                span_per_cluster= 20, retained_spans_per_cluster=5, min_prev_ids=10000, \
                                                ner_to_simplify=(), span_level_feature_extractors=default_span_level_feature_extractors, running_features_size=100, \
                                                prefix_extractors = default_prefix_extractors, dedup=True, max_top_parents=10000, \
                                                span_lfs = [], verbose_snrokel=True, use_synonym_replacement=False, max_ontology_depth=4, \
                                                batch_id_prefix = 0, seen = None, span2idx = None, embedder="minilm", \
                                                clusters = None, label2tf = None, df = None, span2cluster_label = None, label_models = None, auto_create_tokenizer_and_train=True, \
                                                ):
    global clip_model, minilm_model, labse_model
    
    self.ngram2weight = {} if not hasattr(self, 'ngram2weight') else self.ngram2weight
    self.compound = {} if not hasattr(self, 'compound') else self.compound
    self.synonyms = {} if not hasattr(self, 'synonyms') else self.synonyms
    stopword = self.stopword = {} if not hasattr(self, 'stopword') else self.stopword
    if embedder == "clip":
      clip_model = clip_model.to(device)
      minilm_model =  minilm_model.cpu()
      labse_model =  labse_model.cpu()
    elif embedder == "minilm":
      clip_model = clip_model.cpu()
      minilm_model =  minilm_model.to(device)
      labse_model =  labse_model.cpu()
    elif embedder == "labse":
      clip_model = clip_model.cpu()
      minilm_model =  minilm_model.cpu()
      labse_model =  labse_model.to(device)

    if os.path.exists(f"{project_name}.arpa") and (not hasattr(self, 'kenlm_model') or self.kenlm_model is None):
      kenlm_model = self.kenlm_model = kenlm.LanguageModel(f"{project_name}.arpa")
    kenlm_model = self.kenlm_model if hasattr(self, 'kenlm_model') else None
    if kenlm_model is None and auto_create_tokenizer_and_train:
      self.create_tokenizer_and_train(project_name, files, )
      kenlm_model = self.kenlm_model = kenlm.LanguageModel(f"{project_name}.arpa")      
    running_features_per_label = {}
    file_name = files.pop()
    f = open(file_name) 
    domain_stopword_set = set(list(stopwords_set) + list(stopword.keys()))
    prior_line = ""
    batch = []
    retained_batch = []
    curr = ""
    cluster_vecs = None
    curr_date = ""
    curr_position = 0
    next_position = 0
    curr_file_size = os.path.getsize(file_name)
    position = 0
    line = ""
    lineno = -1
    curr_lineno = 0

    #TODO, load the below from an config file
    if seen is None: seen = {}
    if span2idx is None: span2idx = {}
    if clusters is None: clusters = {}
    if label2tf is None: label2tf = {}
    if df is None: df = {}
    if span2cluster_label is None: span2cluster_label = {}
    if label_models is None: label_models = []  
    

    with open(f"{project_name}.jsonl", "w", encoding="utf8") as jsonl_file:
      while True:
        try:
          line = f.readline()
          if line: lineno+=1 
        except:
          line = ""
        if len(line) == 0:
          #print ("reading next")
          if curr: 
            hash_id = hash(curr)
            if not dedup or (hash_id not in seen):
                curr_ents = list(itertools.chain(*[[(e.text, e.label_)] if '||' not in e.text else [(e.text.split("||")[0].strip(), e.label_), (e.text.split("||")[-1].strip(), e.label_)] for e in spacy_nlp(curr).ents]))
                curr_ents = list(set([e for e in curr_ents if e[0]]))
                curr_ents.sort(key=lambda a: len(a[0]), reverse=True)
                batch.append({'file_name': file_name, 'lineno': curr_lineno, 'text': curr, 'ents': curr_ents, 'position':curr_position})
                seen[hash_id] = 1
          prior_line = ""
          curr = ""
          if not files: break
          file_name = files.pop()
          f = open(file_name)
          l = f.readline()
          lineno = 0
          curr_lineno = 0
          curr_date = ""
          curr_position = 0
          curr_file_size = os.path.getsize(file_name)
          position = 0
        position = next_position/curr_file_size
        next_position = next_position + len(line)+1
        line = line.strip().replace("  ", " ")
        if not line: continue
        if len(line) < min_len_for_prefix and len(line) > 0:
          prior_line = prior_line + " " + line
          continue
        line = prior_line+" " + line
        prior_line = ""
        line = line.replace("  ", " ").replace("\t", " ").strip("_ ")

        #turn the file position into a percentage
        if len(line) < max_len_for_prefix:
          ents = list(itertools.chain(*[[(e.text, e.label_)] if '||' not in e.text else [(e.text.split("||")[0].strip(), e.label_), (e.text.split("||")[-1].strip(), e.label_)] for e in spacy_nlp(line).ents]))
          ents = [e for e in ents if e[0]]
          ents = [[a[0], a[1], b] for a, b in Counter(ents).items()]
          for prefix, extract in prefix_extractors:
            extracted_text = extract(self, {'text':line, 'position':position, 'ents':ents}) 
            if extracted_text:
              line = extracted_text
              if curr: 
                curr = curr.replace(". .", ". ").replace("..", ".").replace(":.", ".")
                hash_id = hash(curr)
                if not dedup or (hash_id not in seen):
                  curr_ents = list(itertools.chain(*[[(e.text, e.label_)] if '||' not in e.text else [(e.text.split("||")[0].strip(), e.label_), (e.text.split("||")[-1].strip(), e.label_)] for e in spacy_nlp(curr).ents]))
                  curr_ents = list(set([e for e in curr_ents if e[0]]))
                  curr_ents.sort(key=lambda a: len(a[0]), reverse=True)
                  batch.append({'file_name': file_name, 'lineno': curr_lineno, 'text': curr, 'ents': curr_ents, 'position':curr_position})
                  seen[hash_id] = 1
                curr = ""
                curr_lineno = lineno
                curr_position = position
              break
        if curr: 
          curr = curr +" " + line
        else: 
          curr = line
        curr = curr.replace("  ", " ")

        # process the batches
        if len(batch) >= features_batch_size:
          batch_id_prefix += 1
          retained_batch, span2idx, span2cluster_label, label2tf, df = self.create_span_embeds_and_span2cluster_label(project_name, curr_file_size, jsonl_file_idx, span2idx, batch, \
                                                      retained_batch, jsonl_file,  f"{batch_id_prefix}_", span_lfs,  span2cluster_label, text_span_size, \
                                                      kmeans_batch_size=kmeans_batch_size, epoch = epoch, embed_batch_size=embed_batch_size, min_prev_ids=min_prev_ids, \
                                                      max_ontology_depth=max_ontology_depth, max_top_parents=max_top_parents, do_ontology=True, embedder=embedder, \
                                                      running_features_per_label=running_features_per_label, ner_to_simplify=ner_to_simplify, span_level_feature_extractors=span_level_feature_extractors, \
                                                      running_features_size=running_features_size, label2tf=label2tf, df=df, domain_stopword_set=domain_stopword_set, \
                                                      verbose_snrokel=verbose_snrokel,  span_per_cluster=span_per_cluster, use_synonym_replacement=use_synonym_replacement, )  
          batch = []
      
      # do one last batch and finish processing if there's anything left
      if curr: 
          curr = curr.replace(". .", ". ").replace("..", ".").replace(":.", ".")
          hash_id = hash(curr)
          if not dedup or (hash_id not in seen):
            curr_ents = list(itertools.chain(*[[(e.text, e.label_)] if '||' not in e.text else [(e.text.split("||")[0].strip(), e.label_), (e.text.split("||")[-1].strip(), e.label_)] for e in spacy_nlp(curr).ents]))
            curr_ents = list(set([e for e in curr_ents if e[0]]))
            curr_ents.sort(key=lambda a: len(a[0]), reverse=True)
            batch.append({'file_name': file_name, 'lineno': curr_lineno, 'text': curr, 'ents': curr_ents, 'position':curr_position})
            seen[hash_id] = 1
          curr = ""
          curr_lineno = 0
          curr_position = position
      if batch: 
          batch_id_prefix += 1
          retained_batch, span2idx, span2cluster_label, label2tf, df = self.create_span_embeds_and_span2cluster_label(project_name, curr_file_size, jsonl_file_idx, spanf2idx, batch, \
                                                      retained_batch, jsonl_file,  f"{batch_id_prefix}_", span_lfs,  span2cluster_label, text_span_size, \
                                                      kmeans_batch_size=kmeans_batch_size, epoch = epoch, embed_batch_size=embed_batch_size, min_prev_ids=min_prev_ids,  \
                                                      max_ontology_depth=max_ontology_depth, max_top_parents=max_top_parents, do_ontology=True, embedder=embedder,\
                                                      running_features_per_label=running_features_per_label, ner_to_simplify=ner_to_simplify, span_level_feature_extractors=span_level_feature_extractors, \
                                                      running_features_size=running_features_size, label2tf=label2tf, df=df, domain_stopword_set=domain_stopword_set, \
                                                      verbose_snrokel=verbose_snrokel)  
          batch = []
      

    #now create global labeling functions based on all the labeled data
    #have an option to use a different labeling function, such as regression trees. 
    #we don't necessarily need snorkel lfs after we have labeled the dataset.

    if span_lfs:
      df_train = pd.DataFrame(f"{project_name}.jsonl").shuffle()
      for span_label, lfs, snorkel_label_cardinality, snorkel_epochs in span_lfs:
        applier = PandasLFApplier(lfs=lfs)
        L_train = applier.apply(df=df_train)
        label_models.append(span_label, LabelModel(cardinality=snorkel_label_cardinality,verbose=verbose_snrokel))
        
    return {'clusters': clusters, 'span2cluster_label': span2cluster_label, 'span2idx': span2idx, 'label_models': label_models, \
            'batch_id_prefix': batch_id_prefix, 'seen': seen, 'label2tf': label2tf, 'df': df,}   

  def save_pretrained(self, project_name):
      pickle.dump(self, open(f"{project_name}.pickle", "wb"))
    
  @staticmethod
  def from_pretrained(project_name):
      self = pickle.load(open(f"{project_name}.pickle", "rb"))
      return self

