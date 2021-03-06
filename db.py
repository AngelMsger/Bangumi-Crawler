from datetime import date
from datetime import datetime
from datetime import timedelta

from pymongo import MongoClient, IndexModel, ASCENDING, DESCENDING

from utils import logger, log_duration


# Storage Backend Interface
class DB:
    def truncate_all(self) -> None:
        pass

    def archive(self) -> None:
        pass

    def persist_animes(self, animes) -> None:
        pass

    def persist_reviews(self, media_id, reviews, cursor=None, is_long=True) -> None:
        pass

    def get_all_entrances(self):
        pass

    def get_author_tasks(self):
        pass

    def get_author_watched_media_ids(self, mid):
        pass

    def get_valid_author_ratings_follow_pairs(self):
        pass

    def get_authors_count(self):
        pass

    def get_reviews_count(self, media_id):
        pass

    def push_to_follow(self, mid, season_ids) -> None:
        pass

    def is_need_re_calculate(self, mid):
        pass

    def update_anime_top_matches(self, media_id, top_matches) -> None:
        pass

    def update_author_recommendation(self, mid, top_matches, recommendation) -> None:
        pass


# Persist solution for MongoDB
class MongoDB(DB):
    def truncate_all(self) -> None:
        self.db.animes.remove({})
        self.db.authors.remove({})
        self.db.archives.remove({})

    @log_duration
    def archive(self) -> None:
        today = datetime.combine(date.today(), datetime.min.time())
        if self.db.archives.find_one({'date': today}) is None:
            outdated = self.db.animes.find()
            archives = []
            for anime in outdated:
                logger.info('Archiving for Season:%s...' % anime['season_id'])
                archive = {
                    'season_id': anime['season_id'],
                    'favorites': anime['favorites'],
                    'danmaku_count': anime['danmaku_count'],
                    'reviews_count': self.get_reviews_count(anime['media_id']),
                }
                if 'rating' in anime:
                    archive.update({'rating': anime['rating']})
                archives.append(archive)
            self.db.archives.insert_one({
                'date': today,
                'archives': archives
            })

    def persist_animes(self, animes) -> None:
        for anime in animes:
            self.db.animes.update_one({'season_id': anime['season_id']}, {'$set': anime}, upsert=True)

    def persist_reviews(self, media_id, reviews, cursor=None, is_long=True) -> None:
        for review in reviews:
            author = review.pop('author')
            self.db.authors.update_one({'mid': author['mid']},
                                       {'$set': author, '$push': {'reviews': review}}, upsert=True)
        if cursor is not None:
            reviews_type = 'long' if is_long else 'short'
            self.db.animes.update_one({'media_id': media_id},
                                      {'$set': {'last_%s_reviews_cursor' % reviews_type: cursor}})

    def get_all_entrances(self):
        return [{
            'media_id': anime['media_id'],
            'last_long_reviews_cursor': anime.get('last_long_reviews_cursor', None),
            'last_short_reviews_cursor': anime.get('last_short_reviews_cursor', None)
        } for anime in self.db.animes.find()]

    def get_author_tasks(self):
        threshold = datetime.now() - timedelta(hours=self.conf.CRAWL_AUTHOR_TTL)
        return self.db.authors.find({
            'last_crawl': {'$not': {'$gt': threshold}}
        }, {'mid': 1}).limit(self.conf.CRAWL_AUTHOR_MAX_PER_TIME)

    def get_media_id(self, season_id):
        return self.db.animes.find_one({'season_id': season_id}, {'media_id': 1})['media_id']

    def get_author_watched_media_ids(self, mid):
        author = self.db.authors.find_one({'mid': mid})
        commented = set([review['media_id'] for review in author['reviews']])
        followed = set([self.get_media_id(season_id) for season_id in author.get('follow', [])])
        return commented | followed

    def get_valid_author_ratings_follow_pairs(self):
        return ((author['mid'], [review for review in author['reviews']],
                 [self.get_media_id(season_id) for season_id in author.get('follow', [])])
                for author in self.db.authors.find({
                    '$where': 'this.reviews.length > %s' % self.conf.ANALYZE_AUTHOR_REVIEWS_VALID_THRESHOLD
                }))

    def get_authors_count(self, is_valid=True):
        query = {
            '$where': 'this.reviews.length > %s' % self.conf.ANALYZE_AUTHOR_REVIEWS_VALID_THRESHOLD
        } if is_valid else {}
        return self.db.authors.count(query)

    def get_reviews_count(self, media_id):
        pipeline = [{
            '$project': {'matched': {'$size': {'$filter': {
                'input': '$reviews', 'cond': {'$eq': ['$$this.media_id', media_id]}
            }}}}}, {'$group': {'_id': None, 'matched_size': {'$sum': '$matched'}}}]
        matched_sizes = self.db.authors.aggregate(pipeline)
        reviews_count = 0
        for i in matched_sizes:
            reviews_count += i['matched_size']
        return reviews_count

    def push_to_follow(self, mid, season_ids) -> None:
        self.db.authors.update_one({'mid': mid}, {'$set': {'follow': season_ids, 'last_crawl': datetime.now()}})

    def is_need_re_calculate(self, mid):
        threshold = datetime.now() - timedelta(hours=self.conf.ANALYZE_AUTHOR_TTL)
        last_analyze = self.db.authors.find_one({'mid': mid}).get('last_analyze')
        return last_analyze is None or last_analyze < threshold

    def update_anime_top_matches(self, media_id, top_matches) -> None:
        self.db.animes.update_one({'media_id': media_id}, {'$set': {'top_matches': top_matches}})

    def update_author_recommendation(self, mid, top_matches, recommendation) -> None:
        self.db.authors.update_one({'mid': mid}, {'$set': {
            'top_matches': top_matches,
            'recommendation': recommendation,
            'last_analyze': datetime.now()
        }})

    def __init__(self, conf) -> None:
        self.conf = conf
        self.client = MongoClient(conf.DB_HOST, conf.DB_PORT)
        self.db = self.client[conf.DB_DATABASE]
        if conf.DB_ENABLE_AUTH:
            self.db.authenticate(conf.DB_USERNAME, conf.DB_PASSWORD)

        # Init Index
        collections = set(self.db.collection_names())
        if 'animes' not in collections:
            animes_index_0 = IndexModel([('season_id', ASCENDING)])
            animes_index_1 = IndexModel([('media_id', ASCENDING)])
            self.db.animes.create_indexes([animes_index_0, animes_index_1])
        if 'authors' not in collections:
            authors_index_0 = IndexModel([('mid', ASCENDING)])
            authors_index_1 = IndexModel([('reviews.media_id', ASCENDING)])
            self.db.authors.create_indexes([authors_index_0, authors_index_1])
        if 'archives' not in collections:
            self.db.archives.create_index([('date', DESCENDING), ('archives.media_id', ASCENDING)])

    def __del__(self) -> None:
        self.client.close()


class MySQL(DB):
    pass
