"""Provide access to data stored in FCS files"""

from warnings import warn
from core import FCMdata
from core import Annotation
from core import UnimplementedFcsDataMode
from operator import and_
from math import log
from struct import calcsize, unpack
import re
import numpy


class FCSreader(object):
    """
    class to hold object to read and parse fcs files.  main usage is to get 
    a FCMdata object out of a fcs file
    """
    def __init__(self, filename):
        self.filename = filename
        #self._fh = cStringIO.StringIO(open(filename, 'rb').read())
        self._fh = open(filename, 'rb')
        self.cur_offset = 0
        
    def get_FCMdata(self):
        """Return the next FCM data set stored in a FCS file"""
        # parse headers
        header = self.parse_header(self.cur_offset)
        # parse text 
        text = self.parse_text(self.cur_offset, header['text_start'], header['text_stop'])
        # parse annalysis
        try:
            astart = text['beginanalysis']
        except KeyError:
            astart = header['analysis_start']
        try:
            astop = text['endanalysis']
        except KeyError:
            astop = header['analysis_end']
        analysis = self.parse_analysis(self.cur_offset, astart, astop)
        # parse data
        try:
            dstart = int(text['beginadata'])
        except KeyError:
            dstart = header['data_start']
        try:
            dstop = int(text['enddata'])
        except KeyError:
            dstop = header['data_end']
        
        data = self.parse_data(self.cur_offset, dstart, dstop, text)
        # build fcmdata object
        channels = []
        display = []
        scchannels = []
        for i in range(1,int(text['par'])+1):
            name= text['p%dn' %i]
            channels.append(name)
            try:
                display.append(text['p%ds' %i])
            except KeyError:
                display.append(name)
            if not name.lower().startswith('fl'):
                scchannels.append(name)
            
                
        
        tmpfcm = FCMdata(data, channels, scchannels,
            Annotation({'text': text,
                        'header': header,
                        'analysis': analysis,
                        'display': display}))
        # update cur_offset for multidata files
        # return fcm object
        return tmpfcm
        
    
    def read_bytes(self, offset, start, stop):
        """Read in bytes from start to stop inclusive."""
        self._fh.seek(offset+start)
        return self._fh.read(stop-start+1)
    
    def parse_header(self, offset):
        """
        Parse the FCM data in fcs file at the offset (supporting multiple 
        data segments in a file
        """
        
        header = {}
        header['text_start'] = int(self.read_bytes(offset, 10, 17))
        header['text_stop'] = int(self.read_bytes(offset, 18, 25))
        header['data_start'] = int(self.read_bytes(offset, 26, 33))
        header['data_end'] = int(self.read_bytes(offset, 34, 41))
        header['analysis_start'] = int(self.read_bytes(offset, 42, 49))
        header['analysis_end'] = int(self.read_bytes(offset, 50, 57))
        
        return header
        
    
    def parse_text(self, offset, start, stop):
        """return parsed text segment of fcs file"""
        
        text = self.read_bytes(offset, start, stop)
        #TODO: add support for suplement text segment
        return parse_pairs(text)
    
    def parse_analysis(self, offset, start, stop):
        """return parsed analysis segment of fcs file"""
        
        if start == stop:
            return {}
        else:
            text = self.read_bytes(offset, start, stop)
            return parse_pairs(text)
    
    def parse_data(self, offset, start, stop, text):
        """return numpy.array of data segment of fcs file"""
        
        dtype = text['datatype']
        mode = text['mode']
        tot = int(text['tot'])
        if mode == 'c' or mode == 'u':
            raise UnimplementedFcsDataMode(mode)
        
        if text['byteord'] == '1,2,3,4':
            order = '<'
        elif text['byteord'] == '4,3,2,1':
            order = '>'
        else:
            warn("unsupported byte order, using default @")
            order = '@'
        # from here on out we assume mode l (list)
        
        bitwidth = []
        drange = []
        for i in range(1,int(text['par'])+1):
            bitwidth.append(int(text['p%db' %i]))
            drange.append(int(text['p%dr' %i]))
        
        if dtype.lower() == 'i':
            data = self.parse_int_data(offset, start, stop, bitwidth, drange, tot, order)
        elif dtype.lower() == 'f' or dtype.lower() == 'd':
            data = self.parse_float_data(offset, start, stop, dtype.lower(), tot, order)
        else: # ascii
            data = self.parse_ascii_data(offset, start, stop, bitwidth, dtype, tot, order)
        return data
    
    def parse_int_data(self, offset, start, stop, bitwidth, drange, tot, order):
        """Parse out and return integer list data from fcs file"""
        
        if reduce(and_, [item in [8, 16, 32] for item in bitwidth]):
            if len(set(bitwidth)) == 1: # uniform size for all parameters
                # calculate how much data to read in.
                num_items = (stop-start+1)/calcsize(fmt_integer(bitwidth[0]))
                #unpack into a list
                tmp = unpack('%s%d%s' % (order, num_items, fmt_integer(bitwidth[0])), 
                                    self.read_bytes(offset, start, stop))
                

            else: # parameter sizes are different e.g. 8, 8, 16,8, 32 ... do one at a time
                unused_bitwidths = map(int, map(log2, drange))
                tmp = []
                cur = start
                while cur < stop:
                    for i, curwidth in enumerate(bitwidth):
                        bitmask = mask_integer(curwidth, unused_bitwidths[i])
                        nbytes = curwidth/8
                        bin_string = self.read_bytes(offset, cur, cur+nbytes-1)
                        cur += nbytes
                        val = bitmask & unpack('%s%s' % (order, fmt_integer(curwidth)), bin_string)[0]
                        tmp.append(val)
        else: #non starndard bitwiths...  Does this happen?
            warn('Non-standard bitwidths for data segments')
            return None
        return numpy.array(tmp).reshape((tot, len(bitwidth)))
    
    def parse_float_data(self, offset, start, stop, dtype, tot, order):
        """Parse out and return float list data from fcs file"""
        
        #count up how many to read in
        num_items = (stop-start+1)/calcsize(dtype) 
        # unpack binary data
        tmp = unpack('%s%d%s' % (order, num_items, dtype), self.read_bytes(offset, start, stop))
        return numpy.array(tmp).reshape((tot, len(tmp)/tot))
    
    def parse_ascii_data(self, offset, start, stop, bitwidth, dtype, tot, order):
        """Parse out ascii encoded data from fcs file"""
        
        num_items = (stop-start+1)/calcsize(dtype) 
        tmp = unpack('%s%d%s' % (order, num_items, dtype), self.read_bytes(offset, start, stop))
        return numpy.array(tmp).reshape((tot, len(tmp)/tot))
            
        
def parse_pairs(text):
    """return key/value pairs from a delimited string"""
    
    delim = text[0]
    if delim != text[-1]:
        warn("text in segment does not start and end with delimiter")
    tmp = text[1:-1].replace('$','')
    # match the delimited character unless it's doubled
    regex = re.compile('(?<=[^%s])%s(?!%s)' % (delim, delim, delim))
    tmp = regex.split(tmp)
    return dict(zip([ x.lower() for x in tmp[::2]], tmp[1::2]))
    
def fmt_integer(b):
    """return binary format of an integer"""
    
    if b == 8:
        return 'B'
    elif b == 16:
        return 'H'
    elif b == 32:
        return 'I'
    else:
        print "Cannot handle integers of bit size %d" % b
        return None

def mask_integer(b, ub):
    """return bitmask of an integer and a bitwitdh"""
    
    if b == 8:
        return (0xFF >> (b-ub))
    elif b == 16:
        return (0xFFFF >> (b-ub))
    elif b == 32:
        return (0xFFFFFFFF >> (b-ub))
    else:
        print "Cannot handle integers of bit size %d" % b
        return None

def log_factory(base):
    """constructor of various log based functions"""
    
    def f(x):
        return log(x, base)
    return f

log2 = log_factory(2)

def loadFCS(filename):
    """Load and return a FCM data object from an FCS file"""
    
    tmp = FCSreader(filename)
    return tmp.get_FCMdata()

if __name__ == '__main__':
    pass
