import datetime
import json
import logging
import math
import os
import random
import sys
import time
from collections import OrderedDict

import redis

import geoip2.database
import phonenumbers
import pycountry
import util
from helpers import live_helper
from phonenumbers import geocoder


class InvalidCoordinate(Exception):
    pass


class Geo_helper:
    def __init__(self, serv_redis_db, cfg):
        self.serv_redis_db = serv_redis_db
        self.cfg = cfg
        self.serv_coord = redis.StrictRedis(
                host=cfg.get('RedisGlobal', 'host'),
                port=cfg.getint('RedisGlobal', 'port'),
                db=cfg.getint('RedisMap', 'db'))
        self.live_helper = live_helper.Live_helper(serv_redis_db, cfg)

        #logger
        logDir = cfg.get('Log', 'directory')
        logfilename = cfg.get('Log', 'helpers_filename')
        logPath = os.path.join(logDir, logfilename)
        if not os.path.exists(logDir):
            os.makedirs(logDir)
        try:
            handler = logging.FileHandler(logPath)
        except PermissionError as error:
            print(error)
            print("Please fix the above and try again.")
            sys.exit(126)
        formatter = logging.Formatter('%(asctime)s:%(levelname)s:%(name)s:%(message)s')
        handler.setFormatter(formatter)
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(handler)
        # Conditional logging to stdout.
        log_stdout = cfg.get('Log', 'stdout')
        if log_stdout:
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setFormatter(logging.Formatter("%(asctime)s — %(name)s — %(levelname)s — %(message)s"))
            self.logger.addHandler(console_handler)

        self.keyCategCoord = "GEO_COORD"
        self.keyCategCountry = "GEO_COUNTRY"
        self.keyCategRad = "GEO_RAD"
        self.PATH_TO_DB = cfg.get('RedisMap', 'pathMaxMindDB')
        self.PATH_TO_JSON = cfg.get('RedisMap', 'path_countrycode_to_coord_JSON')
        self.CHANNELDISP = cfg.get('RedisMap', 'channelDisp')

        try:
            self.reader = geoip2.database.Reader(self.PATH_TO_DB)
        except PermissionError as error:
            print(error)
            print("Please fix the above and try again.")
            sys.exit(126)
        self.country_to_iso = {}
        for country in pycountry.countries:
            try:
                self.country_to_iso[country.name] = country.alpha_2
            except AttributeError:
                pass
        with open(self.PATH_TO_JSON) as f:
            self.country_code_to_coord = json.load(f)

    ''' GET '''
    def getTopCoord(self, date):
            topNum = 6 # default Num
            data = util.getZrange(self.serv_redis_db, self.keyCategCoord, date, topNum)
            return data

    def getHitMap(self, date):
        topNum = 0 # all
        data = util.getZrange(self.serv_redis_db, self.keyCategCountry, date, topNum)
        return data

    def getCoordsByRadius(self, dateStart, dateEnd, centerLat, centerLon, radius):
        dico_coord = {}
        to_return = []
        delta = dateEnd - dateStart
        for i in range(delta.days+1):
            correctDatetime = dateStart + datetime.timedelta(days=i)
            date_str = util.getDateStrFormat(correctDatetime)
            keyname = "{}:{}".format(self.keyCategRad, date_str)
            res = self.serv_redis_db.georadius(keyname, centerLon, centerLat, radius, unit='km', withcoord=True)

            #sum up really close coord
            for data, coord in res:
                flag_added = False
                coord = [coord[0], coord[1]]
                #list all coord
                for dicoCoordStr in dico_coord.keys():
                    dicoCoord = json.loads(dicoCoordStr)
                    #if curCoord close to coord
                    if self.isCloseTo(dicoCoord, coord):
                        #add data to dico coord
                        dico_coord[dicoCoordStr].append(data)
                        flag_added = True
                        break
                # coord not in dic
                if not flag_added:
                    dico_coord[str(coord)] = [data]

            for dicoCoord, array in dico_coord.items():
                dicoCoord = json.loads(dicoCoord)
                to_return.append([array, dicoCoord])
        return to_return

    ''' ADD '''
    def getCoordFromIpAndPublish(self, supposed_ip, categ):
        try:
            rep = self.ip_to_coord(supposed_ip)
            coord = rep['coord']
            coord_dic = {'lat': coord['lat'], 'lon': coord['lon']}
            ordDic = OrderedDict() #keep fields with the same layout in redis
            ordDic['lat'] = coord_dic['lat']
            ordDic['lon'] = coord_dic['lon']
            ordDic['categ'] = categ
            ordDic['value'] = supposed_ip
            coord_list = [coord['lat'], coord['lon']]
            if not self.coordinate_list_valid(coord_list):
                raise InvalidCoordinate("Coordinate do not match EPSG:900913 / EPSG:3785 / OSGEO:41001")
            self.push_to_redis_zset(self.keyCategCoord, json.dumps(ordDic))
            iso_code = rep['full_rep'].country.iso_code if rep['full_rep'].country.iso_code is not None else rep['full_rep'].registered_country.iso_code
            country_name = rep['full_rep'].country.name if rep['full_rep'].country.name is not None else rep['full_rep'].registered_country.name
            self.push_to_redis_zset(self.keyCategCountry, iso_code)
            ordDic = OrderedDict() #keep fields with the same layout in redis
            ordDic['categ'] = categ
            ordDic['value'] = supposed_ip
            self.push_to_redis_geo(self.keyCategRad, coord['lon'], coord['lat'], json.dumps(ordDic))
            to_send = {
                    "coord": coord,
                    "categ": categ,
                    "value": supposed_ip,
                    "country": country_name,
                    "specifName": rep['full_rep'].subdivisions.most_specific.name,
                    "cityName": rep['full_rep'].city.name,
                    "regionCode": iso_code,
                    }
            j_to_send = json.dumps(to_send)
            self.serv_coord.publish(self.CHANNELDISP, j_to_send)
            self.live_helper.add_to_stream_log_cache('Map', j_to_send)
            self.logger.info('Published: {}'.format(json.dumps(to_send)))
        except ValueError:
            self.logger.warning("Can't resolve IP: " + str(supposed_ip))
        except geoip2.errors.AddressNotFoundError:
            self.logger.warning("Address not in Database")
        except InvalidCoordinate:
            self.logger.warning("Coordinate do not follow redis specification")


    def getCoordFromPhoneAndPublish(self, phoneNumber, categ):
        try:
            rep = phonenumbers.parse(phoneNumber, None)
            if not (phonenumbers.is_valid_number(rep) or phonenumbers.is_possible_number(rep)):
                self.logger.warning("Phone number not valid")
                return
            country_name = geocoder.country_name_for_number(rep, "en")
            country_code = self.country_to_iso[country_name]
            if country_code is None:
                self.logger.warning("Non matching ISO_CODE")
                return
            coord = self.country_code_to_coord[country_code.lower()]  # countrycode is in upper case
            coord_dic = {'lat': coord['lat'], 'lon': coord['long']}

            ordDic = OrderedDict() #keep fields with the same layout in redis
            ordDic['lat'] = coord_dic['lat']
            ordDic['lon'] = coord_dic['lon']
            coord_list = [coord['lat'], coord['long']]
            if not self.coordinate_list_valid(coord_list):
                raise InvalidCoordinate("Coordinate do not match EPSG:900913 / EPSG:3785 / OSGEO:41001")
            self.push_to_redis_zset(self.keyCategCoord, json.dumps(ordDic))
            self.push_to_redis_zset(self.keyCategCountry, country_code)
            ordDic = OrderedDict() #keep fields with the same layout in redis
            ordDic['categ'] = categ
            ordDic['value'] = phoneNumber
            self.push_to_redis_geo(self.keyCategRad, coord['long'], coord['lat'], json.dumps(ordDic))
            to_send = {
                    "coord": coord_dic,
                    "categ": categ,
                    "value": phoneNumber,
                    "country": country_name,
                    "specifName": "",
                    "cityName": "",
                    "regionCode": country_code,
                    }
            j_to_send = json.dumps(to_send)
            self.serv_coord.publish(self.CHANNELDISP, j_to_send)
            self.live_helper.add_to_stream_log_cache('Map', j_to_send)
            self.logger.info('Published: {}'.format(json.dumps(to_send)))
        except phonenumbers.NumberParseException:
            self.logger.warning("Can't resolve phone number country")
        except InvalidCoordinate:
            self.logger.warning("Coordinate do not follow redis specification")

    ''' UTIL '''
    def push_to_redis_geo(self, keyCateg, lon, lat, content):
        now = datetime.datetime.now()
        today_str = util.getDateStrFormat(now)
        keyname = "{}:{}".format(keyCateg, today_str)
        try:
            self.serv_redis_db.geoadd(keyname, lon, lat, content)
        except redis.exceptions.ResponseError as error:
            print(error)
            print("Please fix the above, and make sure you use a redis version that supports the GEOADD command.")
            print("To test for support: echo \"help GEOADD\"| redis-cli")
        self.logger.debug('Added to redis: keyname={}, lon={}, lat={}, content={}'.format(keyname, lon, lat, content))

    def push_to_redis_zset(self, keyCateg, toAdd, endSubkey="", count=1):
        if not isinstance(toAdd, str):
            self.logger.warning('Can\'t add to redis, element is not of type String. {}'.format(type(toAdd)))
            return
        now = datetime.datetime.now()
        today_str = util.getDateStrFormat(now)
        keyname = "{}:{}{}".format(keyCateg, today_str, endSubkey)
        self.serv_redis_db.zincrby(keyname, count, toAdd)
        self.logger.debug('Added to redis: keyname={}, toAdd={}, count={}'.format(keyname, toAdd, count))

    def ip_to_coord(self, ip):
        resp = self.reader.city(ip)
        try:
            lat = float(resp.location.latitude)
            lon = float(resp.location.longitude)
        except TypeError: # No location, try to use iso_code instead
            self.logger.info('no location in geIP.database response for ip: {}'.format(ip))
            iso_code = resp.registered_country.iso_code #if no iso_code, throws
            coord = self.country_code_to_coord[iso_code.lower()]  # countrycode is in upper case
            lat = float(coord['lat'])
            lon = float(coord['long'])
        # 0.0001 correspond to ~10m
        # Cast the float so that it has the correct float format
        lat_corrected = float("{:.4f}".format(lat))
        lon_corrected = float("{:.4f}".format(lon))
        return { 'coord': {'lat': lat_corrected, 'lon': lon_corrected}, 'full_rep': resp }

    def isCloseTo(self, coord1, coord2):
        clusterMeter = self.cfg.getfloat('GEO' ,'clusteringDistance')
        clusterThres = math.pow(10, len(str(abs(clusterMeter)))-7) #map meter to coord threshold (~ big approx)
        if abs(float(coord1[0]) - float(coord2[0])) <= clusterThres:
            if abs(float(coord1[1]) - float(coord2[1])) <= clusterThres:
                return True
        return False

    # adjust latitude and longitude to fit the limit, as specified
    # by EPSG:900913 / EPSG:3785 / OSGEO:41001
    # coord_list = [lat, lon]
    def coordinate_list_valid(self, coord_list):
        lat = float(coord_list[0])
        lon = float(coord_list[1])
        if (-180 <= lon <= 180) and (-85.05112878 <= lat <= 85.05112878):
            return True
        else:
            return False
