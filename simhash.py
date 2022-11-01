#@title Simhash Code
#simhash hashing and clustering based on Chenghao Mou's awesome: https://github.com/bigscience-workshop/data_tooling/blob/master/ac_dc/deduplicate/ which is under Apache 2
from typing import Dict
import numpy as np
import simhash
import regex as re
from itertools import product
import os, tqdm
from collections import Counter, defaultdict, deque
from typing import Dict, Set
import tqdm 
import random
import itertools

PUNCTUATION_REGEX = re.compile(r"\p{P}")
DIGIT_REGEX = re.compile(r"\d")

def hashing(
    document: str,
    tokenization: str = "character",
    window_size: int = 20,
    ignore_punctuation: bool = True,
    lowercase: bool = True
) -> Dict[str, int]:
    """Hashing a document with SimHash.
    spanmeters
    ----------
    document : str
        The text to use for hashing, by default "text"
    tokenization : str, optional
        Method to use for tokenization, by default "character"
    window_size : int, optional
        The size of the token window, by default 6
    ignore_punctuation : bool, optional
        To ignore punctuation or not, by default True
    lowercase : bool, optional
        To lowercase the text or not, by default True
    Returns
    -------
    int: The hash code

    Raises
    ------
    Exception
        Unrecognized tokenization spanmeter
    """
    if lowercase:
        document = document.lower()

    if ignore_punctuation:
        document = PUNCTUATION_REGEX.sub("", document)

    if tokenization == "character":
        document = " ".join(document.split())
        tokens = [
            str.encode(document[i : i + window_size])
            for i in range(len(document) - window_size)
        ]
    elif tokenization == "punctuation":
        tokens = PUNCTUATION_REGEX.split(document)
        tokens = [
            str.encode(" ".join(tokens[i : i + window_size]))
            for i in range(len(tokens) - window_size)
        ]
    elif tokenization == "space":
        tokens = document.split(" ") #consider whether we want to just use .split() to match \n and \t
        tokens = [
            str.encode(" ".join(tokens[i : i + window_size]))
            for i in range(len(tokens) - window_size)
        ]
    # we could try other types of tokenizations such as stemming and removal of stopwords
    else:
        raise Exception(f"Unrecognized tokenization spanmeter {tokenization}")
    #TODO: the hash code is actually a 64bit int. Check sys.maxsize. 
    #Was having a problem with serialzing np.int64 in json so i casted to int. 
    #might not be an issue in parquet in which case we should revert back to np.int64.
    return int(simhash.compute(map(simhash.unsigned_hash, tokens)))


def find_clusters_batch(visited, hash2cluster, cluster2hash, hashes, num_blocks, hamming_distance):
    matches = simhash.find_all(hashes, num_blocks, hamming_distance)
    graph = defaultdict(dict)
    for x, y in matches:
      graph[x][y] = True
      graph[y][x] = True
    hashes = set(hashes)
    cluster_id: int = 0

    while hashes:
        hash = hashes.pop()
        if hash in visited:
            continue

        # BFS to find the cluster
        if hash not in graph:
            hash2cluster[hash] = -1
            continue

        q = deque([hash])
        visited.add(hash)
        hash2cluster[hash] = cluster_id
        cluster2hash[cluster_id] = cluster2hash.get(cluster_id, []) + [hash]

        while q:
            node = q.popleft()
            for neighbor in graph[node]:
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                q.append(neighbor)
                hash2cluster[neighbor] = cluster_id
                cluster2hash[cluster_id] = cluster2hash.get(cluster_id, []) + [neighbor]

        cluster_id += 1
    return visited, hash2cluster, cluster2hash,

def find_clusters(hashes, num_blocks, hamming_distance, do_sort=True, batch_size=900000, verbose=False):
  if do_sort: 
    hashes.sort()
  # we are assuming no exact duplicates. if we want to deal with exact duplicates, we can easily just collapse them in sequence
  # since this is a sorted list
  cluster2hash = {}
  hash2cluster = {}
  visited: Set[int] = set()
  if len(hashes) <= batch_size:
    visited, hash2cluster, cluster2hash = find_clusters_batch(visited, hash2cluster, cluster2hash, hashes, num_blocks, hamming_distance)
    return hash2cluster, cluster2hash
  batch_size2 = int(batch_size/2)
  if verbose:
    a_iter = tqdm.tqdm(range(0, len(hashes), batch_size2))
  else:
    a_iter = range(0, len(hashes), batch_size2)
  for rng in a_iter:
    max_rng = min(len(hashes), rng+batch_size2)
    hashes2 = hashes[rng:max_rng]
    hashes3 = []
    if cluster2hash:
      iterms_per_clusters = int(max(1, batch_size2/len(cluster2hash)))
      hashes3 = list(itertools.chain(*[val[:iterms_per_clusters] for val in cluster2hash.values()]))
      if len(hashes3) > int(batch_size2/2):
        hashes3 = random.sample(hashes3, batch_size2)
    if rng > 0 and len(hashes3) < batch_size2:
        hashes3 = list(set(hashes3+random.sample(hashes[:rng], batch_size2-len(hashes3))))
    #print (len(hashes3))
    hashes2.extend(hashes3)
    #print (len(hashes2))
    visited, hash2cluster, cluster2hash = find_clusters_batch(visited, hash2cluster, cluster2hash, hashes2, num_blocks, hamming_distance)
  return hash2cluster, cluster2hash

def incremental_span_and_document_neardedup(text, dup_span, dup_doc, shingle_size = 5, cleanup_dup_span_limit=1000000, cleanup_dup_doc_limit=1000000, normalize_text=True, keep_first_dup_in_text=True, replace_char='*'):
    """
    Given a document text, and a hashtable representing any near duplicate spans and duplicate docs, remove duplicate spans of shingle size from the text.
    
    Return:
    
      doc_is_dup, text
        where doc_is_dup is 0 if there is no duplicates, 1 if there are partial span dups, and 2 if the whole document is a near dup.
        text is the original text with any duplicate spans replaced with the replace_char, collapsing multiple replace chars into one char.

    """
    is_dup_chunk={}
    clean_text = text
    if normalize_text:
      #simple normalize and add double spaces after sentences. TODO, add other lang punc.
      clean_text = clean_text.replace("! ", "!  ").replace("? ", "?  ").replace(". ", ".  ").replace("．", "．  ").replace("。", "。  ").replace("？", "？  ")\
        .replace("!\" ", "!\"  ").replace("?\" ", "?\"  ").replace(".\" ", ".\"  ").replace("．\"", "．\"  ").replace("。\"", "。\"  ").replace("？\"", "？\"  ")\
        .replace("!》 ", "!》  ").replace("?》 ", "?》  ").replace(".》 ", ".》  ").replace("．》", "．》  ").replace("。》", "。》  ").replace("？》", "？》  ")\
        .replace("、", "、 ").replace("’s", " 's").replace("`s", " 's").replace("'s", " 's")
    text_arr = [a.strip() for a in clean_text.split("  ") if a.strip()]
    
    #chunkify into sentences
    chunks = []
    for sent in text_arr:
      if not sent: continue
      if " " not in sent and len(sent) > 20:
          while sent:
            chunks.append(sent[:20])
            sent = sent[20:]
      else:
          chunks.append(sent)
    
    replace_text = " "+replace_char+" "
    shingles = [" ".join(chunks[i : i + shingle_size]) for i in range(len(chunks) - shingle_size)]
    is_dup_chunk = {}
    clean_text = " ".join(clean_text.split())
    
    #dedup spans other than the first matching span using shingle_size of sentences (e.g., a span) 
    for ch_idx in range(len(chunks) - shingle_size):
      orig_shingle= " ".join(chunks[ch_idx : ch_idx + shingle_size])
      shingle = DIGIT_REGEX.sub('1', orig_shingle).strip()
      if not shingle: continue
      hashcode = hashing(shingle)
      if hashcode in is_dup_chunk:
        prev_ch_idx = is_dup_chunk[hashcode][0]
        prev_chunk = chunks[prev_ch_idx]
        clean_position = clean_text.find(prev_chunk)
        text_position = text.find(prev_chunk)
        if clean_position >= 0 and text_position >= 0:
          clean_position += len(shingle)
          text_position += len(shingle)
          clean_text2 = clean_text[clean_position+1:]
          text2 = text[text_position+1:]
          if shingle in clean_text2:
            clean_text2 = clean_text2.replace(shingle, replace_text)
          else:
            for chunk in chunks[ch_idx : ch_idx + shingle_size]:
              if len(chunk) > 3: clean_text2 = clean_text2.replace(chunk, replace_text)
          if shingle in text2:
            text2 = text2.replace(shingle, replace_text)
          else:
            for chunk in chunks[ch_idx : ch_idx + shingle_size]:
              if len(chunk) > 3: text2 = text2.replace(chunk, replace_text)
          clean_text = clean_text[:clean_position+1] + clean_text2
          text = text[:text_position+1] + text2
      
      is_dup_chunk[hashcode] = is_dup_chunk.get(hashcode, []) + [ch_idx]
        
      if hashcode in dup_span:
        dup_span[hashcode] += 1
      else:
        dup_span[hashcode] = 1
        
    if not keep_first_dup_in_text:      
      for hashcode, ch_idx in is_dup_chunk.items():  
        if hashcode in dup_span and dup_span[hashcode] > len(ch_idx): #this item is a duplicate across documents
          ch_idx = ch_idx[0]
          shingle= " ".join(chunks[ch_idx : ch_idx + shingle_size])
          if shingle in clean_text: 
            text = text.replace(shingle, replace_text)
          else:
            for chunk in chunks[ch_idx : ch_idx + shingle_size]:
                text = text.replace(chunk, replace_text)
                
    text = text.replace(replace_char+" .", replace_text).\
        replace(replace_char+" !", replace_text).\
        replace(replace_char+" ?", replace_text).\
        replace(replace_char+" .", replace_text).\
        replace(replace_char+" ．", replace_text).\
        replace(replace_char+" 。", replace_text).\
        replace(replace_char+" ？", replace_text).\
        replace("  ", " ").\
        replace(' '+replace_char+' '+replace_char, " "+replace_char).\
        replace(' '+replace_char+' '+replace_char, " "+replace_char)
    text = " ".join(text.split())

    #TODO: improve this so we cleaup by value until we reach the limit
    if len(dup_span) > cleanup_dup_span_limit:
      for key, val in list(dup_span.items()):
        if val <= 1: del dup_span[key]
          
    if len(dup_doc) > cleanup_dup_doc_limit:
      for key, val in list(dup_doc.items()):
        if val <= 1: del dup_doc[key]
          
    doc_is_dup = 0
    if any([a for a in is_dup_chunk.values() if len(a) > 1]):
      clean_text = " ".join(clean_text.replace("*", "").split())
      hashcode = clean_text.strip(' '+replace_char).lower()
      hashcode = DIGIT_REGEX.sub('1', hashcode)
      hashcode = hashing(hashcode)
      if hashcode in dup_doc:
        dup_doc[hashcode] += 1
        doc_is_dup=2
      else:
        dup_doc[hashcode] = 1
        doc_is_dup=1
        
     return doc_is_dup, text

    